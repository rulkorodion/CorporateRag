from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from corporate_rag_ingestion.api.app import create_app
from corporate_rag_ingestion.api.concurrency import StageCancelledError, StageGate
from corporate_rag_ingestion.api.models import (
    APILatency,
    APITelemetry,
    AnswerRequest,
    AnswerResponse,
)
from corporate_rag_ingestion.api.settings import APISettings
from corporate_rag_ingestion.context.models import ContextBuildStats, ContextPackage
from corporate_rag_ingestion.generation.models import (
    LLMGenerationRequest,
    LLMGenerationResult,
)
from corporate_rag_ingestion.generation.ollama import OllamaClient
from corporate_rag_ingestion.generation.service import RAGAnswerService
from corporate_rag_ingestion.generation.settings import GenerationSettings


def empty_context() -> ContextPackage:
    return ContextPackage(
        query="Неизвестный вопрос",
        context_id="context-empty",
        builder_version="test",
        settings_hash="a" * 64,
        token_counter="test",
        token_count=12,
        rendered_context="Источники отсутствуют",
        stats=ContextBuildStats(),
    )


class StreamingBackend:
    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[LLMGenerationRequest] = []

    def generate(self, request: LLMGenerationRequest) -> LLMGenerationResult:
        self.requests.append(request)
        return self._result()

    def stream(self, request, *, on_delta, cancel_event=None):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        for part in [self.content[:5], self.content[5:]]:
            if part:
                on_delta(part)
        return self._result()

    def _result(self) -> LLMGenerationResult:
        return LLMGenerationResult(
            content=self.content,
            model="fake",
            done_reason="stop",
            total_duration_seconds=1.2,
            load_duration_seconds=0.1,
            prompt_eval_count=120,
            prompt_eval_duration_seconds=0.3,
            eval_count=30,
            eval_duration_seconds=0.8,
        )


def test_streaming_service_validates_before_final_and_keeps_metrics() -> None:
    backend = StreamingBackend("Недостаточно данных.")
    service = RAGAnswerService(backend, GenerationSettings(_env_file=None))
    deltas: list[str] = []

    answer = service.generate_stream(empty_context(), on_delta=deltas.append)

    assert "".join(deltas) == "Недостаточно данных."
    assert answer.citation_validation == "valid"
    assert answer.prompt_eval_count == 120
    assert answer.prompt_eval_duration_seconds == 0.3
    assert answer.eval_duration_seconds == 0.8
    assert answer.prompt_estimated_tokens > answer.context_estimated_tokens


def test_streaming_invalid_citations_are_not_left_as_final_answer() -> None:
    context = empty_context().model_copy(
        update={
            "rendered_context": "[1] Проверенный факт",
            "sources": [],
        }
    )
    backend = StreamingBackend("Неподтверждённый вывод [99].")
    service = RAGAnswerService(backend, GenerationSettings(_env_file=None))
    streamed: list[str] = []

    answer = service.generate_stream(context, on_delta=streamed.append)

    assert "[99]" in "".join(streamed)
    assert "[99]" not in answer.answer
    assert answer.citation_validation == "invalid"
    assert answer.citations == []
    assert "streamed_preview_discarded_after_citation_validation" in answer.warnings


def test_ollama_stream_metrics_and_output_limit_are_parsed() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        lines = [
            {
                "model": "qwen3:14b",
                "message": {"role": "assistant", "content": "Ответ "},
                "done": False,
            },
            {
                "model": "qwen3:14b",
                "message": {"role": "assistant", "content": "[1]."},
                "done": True,
                "done_reason": "stop",
                "total_duration": 2_000_000_000,
                "load_duration": 100_000_000,
                "prompt_eval_count": 90,
                "prompt_eval_duration": 400_000_000,
                "eval_count": 10,
                "eval_duration": 900_000_000,
            },
        ]
        return httpx.Response(
            200,
            text="\n".join(json.dumps(item) for item in lines) + "\n",
        )

    settings = GenerationSettings(_env_file=None)
    deltas: list[str] = []
    with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client.stream(
            LLMGenerationRequest(
                system_prompt="system", user_prompt="user", num_predict=768
            ),
            on_delta=deltas.append,
        )

    assert captured["stream"] is True
    assert captured["options"]["num_predict"] == 768
    assert "".join(deltas) == "Ответ [1]."
    assert result.prompt_eval_count == 90
    assert result.prompt_eval_duration_seconds == 0.4
    assert result.eval_count == 10
    assert result.eval_duration_seconds == 0.9
    assert result.load_duration_seconds == 0.1
    assert result.total_duration_seconds == 2.0


def test_stage_gate_reports_queue_position_wait_and_cancellation() -> None:
    gate = StageGate("llm", limit=1, max_queue=2, timeout=2)
    holder_entered = threading.Event()
    release_holder = threading.Event()

    def holder() -> None:
        with gate.acquire():
            holder_entered.set()
            release_holder.wait(2)

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    assert holder_entered.wait(1)

    cancel = threading.Event()
    queued = threading.Event()
    positions: list[int] = []
    errors: list[Exception] = []

    def waiter() -> None:
        try:
            with gate.acquire(
                cancel_event=cancel,
                on_queued=lambda position: (positions.append(position), queued.set()),
            ):
                pass
        except Exception as exc:  # pragma: no branch - assertion below
            errors.append(exc)

    waiter_thread = threading.Thread(target=waiter)
    waiter_thread.start()
    assert queued.wait(1)
    cancel.set()
    waiter_thread.join(1)
    release_holder.set()
    holder_thread.join(1)

    assert positions == [1]
    assert isinstance(errors[0], StageCancelledError)
    assert gate.snapshot()["total_cancelled"] == 1
    assert gate.snapshot()["queued"] == 0


def test_stage_gate_reports_queue_position_as_fifo_advances() -> None:
    gate = StageGate("llm", limit=1, max_queue=3, timeout=2)
    holder_entered = threading.Event()
    release_holder = threading.Event()
    first_acquired = threading.Event()
    release_first = threading.Event()
    second_acquired = threading.Event()
    first_positions: list[int] = []
    second_positions: list[int] = []

    def holder() -> None:
        with gate.acquire():
            holder_entered.set()
            release_holder.wait(2)

    def first_waiter() -> None:
        with gate.acquire(on_queued=first_positions.append):
            first_acquired.set()
            release_first.wait(2)

    def second_waiter() -> None:
        with gate.acquire(on_queued=second_positions.append):
            second_acquired.set()

    holder_thread = threading.Thread(target=holder)
    first_thread = threading.Thread(target=first_waiter)
    second_thread = threading.Thread(target=second_waiter)
    holder_thread.start()
    assert holder_entered.wait(1)
    first_thread.start()
    deadline = time.monotonic() + 1
    while gate.snapshot()["queued"] != 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    second_thread.start()
    deadline = time.monotonic() + 1
    while gate.snapshot()["queued"] != 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    release_holder.set()
    assert first_acquired.wait(1)
    deadline = time.monotonic() + 1
    while second_positions != [2, 1] and time.monotonic() < deadline:
        time.sleep(0.01)
    release_first.set()
    assert second_acquired.wait(1)

    holder_thread.join(1)
    first_thread.join(1)
    second_thread.join(1)

    assert first_positions == [1]
    assert second_positions == [2, 1]


@pytest.mark.parametrize(
    ("embedding", "reranker", "llm", "ollama"),
    [(1, 1, 1, 1), (2, 1, 1, 1), (2, 2, 1, 1), (2, 2, 2, 2)],
)
def test_supported_concurrency_configs(
    embedding: int, reranker: int, llm: int, ollama: int
) -> None:
    configured = APISettings(
        _env_file=None,
        embedding_concurrency=embedding,
        reranker_concurrency=reranker,
        llm_concurrency=llm,
        ollama_num_parallel=ollama,
    )
    assert configured.llm_concurrency == llm


def test_llm_two_requires_controlled_ollama_parallel_two() -> None:
    with pytest.raises(ValueError, match="OLLAMA_NUM_PARALLEL"):
        APISettings(
            _env_file=None,
            llm_concurrency=2,
            ollama_num_parallel=1,
        )


def test_warmup_retry_backoff_must_not_shrink() -> None:
    with pytest.raises(ValueError, match="WARMUP_RETRY_MAX_SECONDS"):
        APISettings(
            _env_file=None,
            warmup_retry_initial_seconds=30,
            warmup_retry_max_seconds=5,
        )


class StreamingRuntime:
    def __init__(self) -> None:
        self.warmups = 0
        self.streams = 0

    def warmup(self) -> None:
        self.warmups += 1

    def readiness(self) -> dict[str, Any]:
        return {"ready": True}

    def gate_snapshots(self) -> dict[str, dict[str, Any]]:
        return {}

    def stream_answer(self, request_id, request, *, emit, cancel_event):  # type: ignore[no-untyped-def]
        self.streams += 1
        emit({"type": "status", "stage": "llm", "status": "generating"})
        emit({"type": "delta", "delta": "Ответ"})
        return AnswerResponse(
            request_id=request_id,
            answer="Ответ",
            status="completed",
            citation_validation="valid",
            latency=APILatency(ttft_seconds=0.1, total_seconds=0.2),
            telemetry=APITelemetry(
                prompt_eval_count=50,
                eval_count=5,
                context_estimated_tokens=20,
            ),
            model="fake",
            context_id="context",
        )

    def close(self) -> None:
        pass


def test_streaming_endpoint_protocol_and_runtime_are_reused() -> None:
    runtime = StreamingRuntime()
    app = create_app(
        settings=APISettings(_env_file=None, warmup_on_startup=True),
        runtime_factory=lambda _settings: runtime,
    )
    with TestClient(app) as client:
        first = client.post("/api/answer/stream", json={"question": "Первый"})
        second = client.post("/api/answer/stream", json={"question": "Второй"})

    first_events = [json.loads(line) for line in first.text.splitlines()]
    assert [event["type"] for event in first_events] == [
        "status",
        "status",
        "delta",
        "final",
    ]
    assert first_events[-1]["data"]["telemetry"]["prompt_eval_count"] == 50
    assert second.status_code == 200
    assert runtime.warmups == 1
    assert runtime.streams == 2


def test_context_and_output_defaults_remain_unmodified() -> None:
    api = APISettings(_env_file=None)
    generation = GenerationSettings(_env_file=None)
    assert api.default_max_context_tokens == 6000
    assert api.default_max_chunks == 8
    assert generation.num_predict is None
