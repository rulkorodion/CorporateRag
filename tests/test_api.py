from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient

from corporate_rag_ingestion.api.app import create_app
from corporate_rag_ingestion.api.concurrency import (
    StageGate,
    StageQueueFullError,
    StageQueueTimeoutError,
)
from corporate_rag_ingestion.api.models import (
    APILatency,
    AnswerRequest,
    AnswerResponse,
)
from corporate_rag_ingestion.api.settings import APISettings
from corporate_rag_ingestion.api.runtime import ProductionRAGRuntime
from corporate_rag_ingestion.config import Settings as ProjectSettings
from corporate_rag_ingestion.context.models import RelatedImage
from corporate_rag_ingestion.generation.base import LLMUnavailableError
from corporate_rag_ingestion.generation.models import (
    CitationReference,
    SourceAttachment,
    UsedSource,
)
from corporate_rag_ingestion.generation.settings import GenerationSettings
from corporate_rag_ingestion.retrieval.settings import RetrievalSettings


class FakeRuntime:
    def __init__(self, *, delay: float = 0.0, ready: bool = True) -> None:
        self.delay = delay
        self.is_ready = ready
        self.warmups = 0
        self.answers = 0
        self.closed = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def warmup(self) -> None:
        self.warmups += 1

    def readiness(self) -> dict[str, Any]:
        return {"ready": self.is_ready, "qdrant": {"ok": self.is_ready}}

    def gate_snapshots(self) -> dict[str, dict[str, Any]]:
        return {"llm": {"limit": 1, "active": self.active, "queued": 0}}

    def answer(self, request_id: str, request: AnswerRequest) -> AnswerResponse:
        with self.lock:
            self.answers += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            source = UsedSource(
                citation="[1]",
                source_number=1,
                chunk_id="chunk-1",
                document_id="doc-1",
                version_id="version-1",
                relative_path="folder/manual.pdf",
                page_start=2,
                page_end=2,
                heading_path=["Раздел"],
                section_title="Раздел",
                source_type="native",
                provenance={"source_fallback": "native_page_text"},
                related_image_ids=["visual-1"],
            )
            return AnswerResponse(
                request_id=request_id,
                answer="Ответ [1].",
                status="completed",
                citation_validation="valid",
                citations=[
                    CitationReference(
                        citation="[1]",
                        source_number=1,
                        chunk_id="chunk-1",
                        document_id="doc-1",
                        relative_path="folder/manual.pdf",
                        page_start=2,
                        page_end=2,
                        section_title="Раздел",
                    )
                ],
                sources=[source],
                source_attachments=[
                    SourceAttachment(
                        source_number=1,
                        display_relative_path="folder/manual.pdf",
                        download_relative_path="folder/manual.pdf",
                        indexed_relative_path="folder/manual.pdf",
                        indexed_document_id="doc-1",
                    )
                ],
                related_images=[
                    RelatedImage(
                        asset_id="visual-1",
                        document_id="doc-1",
                        version_id="version-1",
                        local_path="folder/visual.png",
                        visual_role="standalone_visual",
                        visual_classification_status="pending",
                        path_scope="knowledge_base",
                    )
                ],
                warnings=[],
                latency=APILatency(
                    retrieval_seconds=0.01,
                    reranker_seconds=0.02,
                    context_builder_seconds=0.003,
                    llm_queue_wait_seconds=0.04,
                    generation_seconds=0.05,
                    total_seconds=0.123,
                ),
                model="fake",
                context_id="context-1",
            )
        finally:
            with self.lock:
                self.active -= 1

    def close(self) -> None:
        self.closed += 1


class ErrorRuntime(FakeRuntime):
    def answer(self, request_id: str, request: AnswerRequest) -> AnswerResponse:
        raise LLMUnavailableError("Ollama unavailable")


class QueueErrorRuntime(FakeRuntime):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def answer(self, request_id: str, request: AnswerRequest) -> AnswerResponse:
        raise self.error


class QdrantErrorRuntime(FakeRuntime):
    def answer(self, request_id: str, request: AnswerRequest) -> AnswerResponse:
        raise RuntimeError("Qdrant unavailable")


class WarmupErrorRuntime(FakeRuntime):
    def warmup(self) -> None:
        self.warmups += 1
        self.is_ready = False
        raise RuntimeError("startup dependency unavailable")


class RecoveringWarmupRuntime(FakeRuntime):
    def __init__(self) -> None:
        super().__init__(ready=False)

    def warmup(self) -> None:
        self.warmups += 1
        if self.warmups == 1:
            raise ConnectionRefusedError("dependency is still starting")
        self.is_ready = True


def settings(**updates: Any) -> APISettings:
    return APISettings(
        _env_file=None,
        warmup_on_startup=True,
        **updates,
    )


def test_production_runtime_respects_configured_gpu_devices(tmp_path) -> None:
    runtime = ProductionRAGRuntime(
        api_settings=APISettings(_env_file=None, warmup_on_startup=False),
        project_settings=ProjectSettings(
            _env_file=None,
            knowledge_base_path=tmp_path / "knowledge-base",
            storage_path=tmp_path / "storage",
            chunk_storage_path=tmp_path / "chunks",
        ),
        retrieval_settings=RetrievalSettings(
            _env_file=None,
            qdrant_path=tmp_path / "qdrant",
            dense_device="cuda",
            reranker_device="cuda",
        ),
        generation_settings=GenerationSettings(_env_file=None),
    )
    try:
        assert runtime.retrieval_settings.dense_device == "cuda"
        assert runtime.retrieval_settings.reranker_device == "cuda"
        assert runtime.retrieval_settings.reranker_release_dense_before_load is False
    finally:
        runtime.close()


def test_health_ready_answer_and_single_lifecycle() -> None:
    runtime = FakeRuntime()
    app = create_app(settings=settings(), runtime_factory=lambda _: runtime)
    with TestClient(app) as client:
        health = client.get("/health")
        ready = client.get("/ready")
        response = client.post(
            "/api/answer",
            headers={"X-Request-ID": "request-123"},
            json={"question": "Как выполнить действие?"},
        )
        assert runtime.warmups == 1
        assert health.status_code == 200
        assert health.json()["metrics"]["active_requests"] >= 1
        assert ready.status_code == 200
        assert response.status_code == 200
        payload = response.json()
        assert payload["request_id"] == "request-123"
        assert payload["citations"][0]["chunk_id"] == "chunk-1"
        assert payload["sources"][0]["provenance"]["source_fallback"] == "native_page_text"
        assert payload["source_attachments"][0]["download_relative_path"] == "folder/manual.pdf"
        assert payload["related_images"][0]["asset_id"] == "visual-1"
        assert payload["latency"]["llm_queue_wait_seconds"] == 0.04
        assert response.headers["X-Request-ID"] == "request-123"
    assert runtime.closed == 1
    assert runtime.answers == 1


def test_answer_telemetry_is_persisted_and_exposed_as_aggregates(tmp_path) -> None:
    runtime = FakeRuntime()
    app = create_app(
        settings=settings(
            telemetry_storage_enabled=True,
            telemetry_database_path=tmp_path / "telemetry" / "requests.db",
        ),
        runtime_factory=lambda _: runtime,
    )
    with TestClient(app) as client:
        answer = client.post(
            "/api/answer",
            headers={"X-Request-ID": "persisted-request"},
            json={"question": "Текст не должен попасть в SQLite"},
        )
        summary = client.get("/api/analytics/summary", params={"days": 7})

    assert answer.status_code == 200
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["totals"]["requests"] == 1
    assert payload["totals"]["completed"] == 1
    assert payload["latency"]["total_seconds"]["average"] == 0.123
    assert payload["privacy"]["stores_question_text"] is False


def test_failed_answer_is_persisted_without_error_message(tmp_path) -> None:
    runtime = ErrorRuntime()
    app = create_app(
        settings=settings(
            telemetry_storage_enabled=True,
            telemetry_database_path=tmp_path / "telemetry" / "requests.db",
        ),
        runtime_factory=lambda _: runtime,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        failed = client.post("/api/answer", json={"question": "Вопрос"})
        summary = client.get("/api/analytics/summary", params={"days": 7})

    assert failed.status_code == 503
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["totals"]["failed"] == 1
    assert payload["errors"][0]["error"] == "ollama_unavailable"
    assert payload["errors"][0]["stage"] == "llm"


def test_ready_returns_503_when_dependency_is_unavailable() -> None:
    runtime = FakeRuntime(ready=False)
    app = create_app(settings=settings(), runtime_factory=lambda _: runtime)
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["ready"] is False


def test_warmup_failure_keeps_health_live_and_ready_unavailable() -> None:
    runtime = WarmupErrorRuntime()
    app = create_app(settings=settings(), runtime_factory=lambda _: runtime)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
    assert runtime.warmups == 1
    assert runtime.closed == 1


def test_warmup_recovers_automatically_after_dependency_startup_race() -> None:
    runtime = RecoveringWarmupRuntime()
    app = create_app(
        settings=settings(
            warmup_retry_initial_seconds=0.1,
            warmup_retry_max_seconds=0.2,
        ),
        runtime_factory=lambda _: runtime,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/ready").status_code == 503
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if client.get("/ready").status_code == 200:
                break
            time.sleep(0.02)
        assert client.get("/ready").json()["ready"] is True
        assert runtime.warmups == 2
        time.sleep(0.25)
        assert runtime.warmups == 2

    assert runtime.closed == 1


def test_concurrent_requests_reuse_one_runtime_instance() -> None:
    runtime = FakeRuntime(delay=0.05)
    app = create_app(settings=settings(), runtime_factory=lambda _: runtime)
    with TestClient(app) as client:
        with ThreadPoolExecutor(max_workers=5) as pool:
            responses = list(
                pool.map(
                    lambda index: client.post(
                        "/api/answer", json={"question": f"Вопрос {index}"}
                    ),
                    range(5),
                )
            )
    assert all(response.status_code == 200 for response in responses)
    assert runtime.warmups == 1
    assert runtime.answers == 5
    assert runtime.max_active > 1
    assert runtime.closed == 1


def test_ollama_unavailable_maps_to_503() -> None:
    runtime = ErrorRuntime()
    app = create_app(settings=settings(), runtime_factory=lambda _: runtime)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/answer", json={"question": "Вопрос"})
    assert response.status_code == 503
    assert response.json()["error"] == "ollama_unavailable"
    assert response.json()["stage"] == "llm"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (StageQueueFullError("llm", "full"), 429, "queue_full"),
        (StageQueueTimeoutError("llm", "timeout"), 503, "queue_timeout"),
    ],
)
def test_queue_errors_have_explicit_http_responses(
    error: Exception, status: int, code: str
) -> None:
    runtime = QueueErrorRuntime(error)
    app = create_app(settings=settings(), runtime_factory=lambda _: runtime)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/answer", json={"question": "Вопрос"})
    assert response.status_code == status
    assert response.json()["error"] == code
    assert response.json()["stage"] == "llm"


def test_qdrant_failure_is_a_diagnostic_503() -> None:
    runtime = QdrantErrorRuntime()
    app = create_app(settings=settings(), runtime_factory=lambda _: runtime)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/answer", json={"question": "Вопрос"})
    assert response.status_code == 503
    assert response.json()["error"] == "pipeline_unavailable"
    assert "Qdrant unavailable" not in response.json()["message"]
    assert "временно недоступна" in response.json()["message"]


def test_legacy_path_based_file_endpoints_are_not_exposed() -> None:
    runtime = FakeRuntime()
    app = create_app(settings=settings(), runtime_factory=lambda _: runtime)
    with TestClient(app) as client:
        source = client.get("/api/files/source", params={"path": "folder/manual.pdf"})
        asset = client.get(
            "/api/files/asset", params={"path": "assets/visual.png", "scope": "storage"}
        )
    assert source.status_code == 404
    assert asset.status_code == 404


def test_cors_is_restricted_to_configured_frontend_origin() -> None:
    runtime = FakeRuntime()
    app = create_app(
        settings=settings(cors_allowed_origins="http://127.0.0.1:3001"),
        runtime_factory=lambda _: runtime,
    )
    with TestClient(app) as client:
        allowed = client.options(
            "/api/answer",
            headers={
                "Origin": "http://127.0.0.1:3001",
                "Access-Control-Request-Method": "POST",
            },
        )
        denied = client.options(
            "/api/answer",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:3001"
    assert "access-control-allow-origin" not in denied.headers


def test_cors_wildcard_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not contain"):
        settings(cors_allowed_origins="*")


def test_stage_gate_fifo_queue_and_overflow() -> None:
    gate = StageGate("llm", limit=1, max_queue=1, timeout=1.0)
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with gate.acquire():
            entered.set()
            release.wait(1)

    first = threading.Thread(target=holder)
    first.start()
    assert entered.wait(1)

    waiter_entered = threading.Event()

    def waiter() -> None:
        with gate.acquire():
            waiter_entered.set()

    second = threading.Thread(target=waiter)
    second.start()
    deadline = time.monotonic() + 1
    while gate.snapshot()["queued"] != 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    with pytest.raises(StageQueueFullError):
        with gate.acquire():
            pass
    release.set()
    first.join(1)
    second.join(1)
    assert waiter_entered.is_set()
    snapshot = gate.snapshot()
    assert snapshot["total_rejected"] == 1
    assert snapshot["max_observed_queue"] == 1


def test_stage_gate_wait_timeout() -> None:
    gate = StageGate("llm", limit=1, max_queue=1, timeout=0.03)
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with gate.acquire():
            entered.set()
            release.wait(1)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(1)
    with pytest.raises(StageQueueTimeoutError):
        with gate.acquire():
            pass
    release.set()
    thread.join(1)
    assert gate.snapshot()["total_timed_out"] == 1
