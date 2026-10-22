from __future__ import annotations

import logging
import hashlib
import threading
import time
from dataclasses import dataclass
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from ..config import PROJECT_ROOT, Settings
from ..context.builder import ContextBuilder
from ..context.models import ContextPackage
from ..context.repository import AssetRepository, ChunkRepository
from ..context.settings import ContextBuilderSettings
from ..generation.models import RAGAnswer
from ..generation.base import LLMError
from ..generation.ollama import OllamaClient
from ..generation.service import RAGAnswerService
from ..generation.settings import GenerationSettings
from ..retrieval.dense import QwenDenseEmbedder
from ..retrieval.rerank_service import RerankSearchService
from ..retrieval.reranker import QwenCrossEncoderReranker
from ..retrieval.search import HybridSearchService
from ..retrieval.settings import RetrievalSettings
from ..retrieval.sparse import BM25SparseEmbedder
from ..retrieval.store import QdrantStore
from ..rolling_schedule.answer import RollingScheduleAnswerService
from ..rolling_schedule.models import ScheduleQuery
from ..rolling_schedule.search import is_schedule_candidate
from ..rolling_schedule.settings import RollingScheduleSettings
from ..ui.presentation import NormalizedPresentationRepository
from .concurrency import StageCancelledError, StageGate
from .models import APILatency, APITelemetry, AnswerRequest, AnswerResponse
from .settings import APISettings


LOGGER = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")


def _paginated_schedule_query(request: AnswerRequest, question: str) -> ScheduleQuery:
    selection = request.schedule_selection
    if selection is None or request.schedule_page is None:
        raise ValueError("complete schedule pagination state is required")
    return ScheduleQuery(
        raw=question,
        is_schedule_intent=True,
        sheet_names=tuple(selection.sheet_names),
        record_kinds=tuple(selection.record_kinds),
        product_kinds=tuple(selection.product_kinds),
        dimensions=tuple(selection.dimensions),
        steel=selection.steel,
        date_from=selection.date_from,
        date_to=selection.date_to,
        date_explicit=selection.date_explicit,
        period_mode=selection.period_mode,
        include_undated=selection.include_undated,
        products_only=selection.products_only,
        query_type=selection.query_type,
        response_mode="full",
        page=request.schedule_page,
    )


class APIAnswerRuntime(Protocol):
    def warmup(self) -> None: ...

    def readiness(self) -> dict[str, Any]: ...

    def answer(self, request_id: str, request: AnswerRequest) -> AnswerResponse: ...

    def stream_answer(
        self,
        request_id: str,
        request: AnswerRequest,
        *,
        emit: Callable[[dict[str, Any]], None],
        cancel_event: threading.Event,
    ) -> AnswerResponse: ...

    def close(self) -> None: ...


@dataclass
class RuntimeGates:
    embedding: StageGate
    reranker: StageGate
    llm: StageGate


@dataclass
class PreparedRequest:
    context: ContextPackage
    latency: APILatency
    telemetry: APITelemetry
    total_started: float


class ProductionRAGRuntime:
    """Single-process production runtime with independently gated heavy stages."""

    def __init__(
        self,
        *,
        api_settings: APISettings,
        project_settings: Settings | None = None,
        retrieval_settings: RetrievalSettings | None = None,
        generation_settings: GenerationSettings | None = None,
    ) -> None:
        self.api_settings = api_settings
        self.project_settings = project_settings or Settings()
        base_retrieval = retrieval_settings or RetrievalSettings()
        # API workers keep both models resident, but the configured devices are
        # respected. Production can therefore use CUDA while tests/dev retain
        # their explicit CPU or auto settings.
        self.retrieval_settings = base_retrieval.model_copy(
            update={
                "reranker_release_dense_before_load": False,
            }
        ).resolve_paths(PROJECT_ROOT)
        self.generation_settings = generation_settings or GenerationSettings()
        self.store = QdrantStore(self.retrieval_settings)
        self.dense = QwenDenseEmbedder(self.retrieval_settings)
        self.sparse = BM25SparseEmbedder(self.retrieval_settings)
        self.hybrid = HybridSearchService(
            self.retrieval_settings, self.store, self.dense, self.sparse
        )
        self.reranker = QwenCrossEncoderReranker(self.retrieval_settings)
        self.rerank_service = RerankSearchService(
            self.retrieval_settings, self.hybrid, self.reranker
        )
        self.chunks = ChunkRepository(self.project_settings.chunk_storage_root)
        self.assets = AssetRepository(
            self.project_settings.normalized_root,
            self.project_settings.knowledge_base_root,
        )
        self.ollama = OllamaClient(self.generation_settings)
        self.generator = RAGAnswerService(self.ollama, self.generation_settings)
        self.presentation = NormalizedPresentationRepository(
            self.project_settings.normalized_root
        )
        self.rolling_schedule_settings = RollingScheduleSettings.from_environment(
            self.project_settings.storage_root
        )
        self.rolling_schedule = RollingScheduleAnswerService(
            self.rolling_schedule_settings
        )
        gate_args = {
            "max_queue": api_settings.max_queue_size,
            "timeout": api_settings.queue_wait_timeout_seconds,
        }
        self.gates = RuntimeGates(
            embedding=StageGate(
                "embedding", limit=api_settings.embedding_concurrency, **gate_args
            ),
            reranker=StageGate(
                "reranker", limit=api_settings.reranker_concurrency, **gate_args
            ),
            llm=StageGate(
                "llm", limit=api_settings.llm_concurrency, **gate_args
            ),
        )
        self._closed = False
        self._ready = False
        self._warmup_error: str | None = None

    def warmup(self) -> None:
        try:
            self.store.ensure_collection()
            with self.gates.embedding.acquire():
                self.dense.embed_query("проверка готовности поиска")
            with self.gates.reranker.acquire():
                _ = self.reranker.runtime
            self._probe_ollama()
            self._ready = True
            self._warmup_error = None
        except Exception as exc:
            self._ready = False
            self._warmup_error = f"{type(exc).__name__}: {exc}"
            raise

    def _probe_ollama(self) -> None:
        response = self.ollama._client.get("/api/tags")
        response.raise_for_status()
        names = {item.get("name") for item in response.json().get("models", [])}
        if self.generation_settings.model not in names:
            raise RuntimeError(
                f"Ollama model is unavailable: {self.generation_settings.model}"
            )

    def readiness(self) -> dict[str, Any]:
        checks: dict[str, Any] = {
            "initialized": self._ready and not self._closed,
            "warmup_error": self._warmup_error,
        }
        try:
            checks["qdrant"] = {
                "ok": self.store.client.collection_exists(
                    self.retrieval_settings.collection_name
                ),
                "collection": self.retrieval_settings.collection_name,
            }
        except Exception as exc:
            checks["qdrant"] = {"ok": False, "error": str(exc)}
        dense_runtime = getattr(self.dense, "_runtime", None)
        reranker_runtime = getattr(self.reranker, "_runtime", None)
        checks["embedding"] = {
            "ok": getattr(self.dense, "_model", None) is not None,
            "model": self.retrieval_settings.dense_model_id,
            "device": getattr(dense_runtime, "device", None),
            "dtype": getattr(dense_runtime, "dtype", None),
        }
        checks["reranker"] = {
            "ok": getattr(self.reranker, "_model", None) is not None,
            "model": self.retrieval_settings.reranker_model_id,
            "device": getattr(reranker_runtime, "device", None),
            "dtype": getattr(reranker_runtime, "dtype", None),
        }
        try:
            self._probe_ollama()
            checks["ollama"] = {
                "ok": True,
                "model": self.generation_settings.model,
                "base_url": self.generation_settings.base_url,
            }
        except Exception as exc:
            checks["ollama"] = {"ok": False, "error": str(exc)}
        schedule_status = self.rolling_schedule.store.status()
        checks["rolling_schedules"] = {
            "enabled": self.rolling_schedule_settings.enabled,
            "available": self.rolling_schedule.store.available,
            "status": schedule_status.get("status", "not_loaded"),
            "last_successful_download_at": schedule_status.get(
                "last_successful_download_at"
            ),
            "last_error": schedule_status.get("last_error"),
        }
        checks["ready"] = bool(
            checks["initialized"]
            and checks["qdrant"].get("ok")
            and checks["embedding"]["ok"]
            and checks["reranker"]["ok"]
            and checks["ollama"].get("ok")
        )
        return checks

    def answer(self, request_id: str, request: AnswerRequest) -> AnswerResponse:
        question = request.question.strip()
        schedule_query = None
        pagination_only = request.schedule_page is not None
        if self.rolling_schedule_settings.enabled and pagination_only:
            schedule_query = _paginated_schedule_query(request, question)
        elif self.rolling_schedule_settings.enabled and is_schedule_candidate(question):
            today = datetime.now(MOSCOW).date()
            analysis_content = None
            try:
                with self.gates.llm.acquire():
                    analysis_content = self.ollama.generate(
                        self.rolling_schedule.intent_request(question, today=today)
                    ).content
            except LLMError:
                LOGGER.warning(
                    "rolling schedule intent analysis failed; using deterministic parser",
                    exc_info=True,
                )
            schedule_query = self.rolling_schedule.resolve_query(
                question, today=today, analysis_content=analysis_content
            )
        schedule_plan = (
            self.rolling_schedule.prepare(
                request_id,
                question,
                num_predict=request.num_predict,
                query=schedule_query,
                expected_snapshot_version=request.schedule_snapshot_version,
                pagination_only=pagination_only,
            )
            if schedule_query is not None
            else None
        )
        if schedule_plan is not None:
            if schedule_plan.llm_request is None:
                return schedule_plan.fallback
            try:
                with self.gates.llm.acquire() as lease:
                    started = time.perf_counter()
                    generated = self.ollama.generate(schedule_plan.llm_request)
                    generation_seconds = time.perf_counter() - started
            except LLMError:
                LOGGER.warning(
                    "rolling schedule answer generation failed; returning grounded fallback",
                    exc_info=True,
                )
                return schedule_plan.fallback
            return self.rolling_schedule.finish(
                schedule_plan,
                generated,
                llm_queue_wait_seconds=lease.wait_seconds,
                llm_queue_position=lease.initial_queue_position,
                generation_seconds=generation_seconds,
            )
        prepared = self._prepare(request, emit=lambda _event: None, cancel_event=None)
        with self.gates.llm.acquire() as lease:
            prepared.latency.llm_queue_wait_seconds = lease.wait_seconds
            prepared.telemetry.llm_queue_position = lease.initial_queue_position
            started = time.perf_counter()
            generated = self.generator.generate(
                prepared.context,
                retrieval_context_seconds=self._retrieval_context_seconds(
                    prepared.latency
                ),
                num_predict=request.num_predict,
            )
            prepared.latency.generation_seconds = time.perf_counter() - started
        return self._response(request_id, prepared, generated)

    def stream_answer(
        self,
        request_id: str,
        request: AnswerRequest,
        *,
        emit: Callable[[dict[str, Any]], None],
        cancel_event: threading.Event,
    ) -> AnswerResponse:
        self._raise_if_cancelled(cancel_event, "rolling_schedule")
        schedule_plan = None
        question = request.question.strip()
        pagination_only = request.schedule_page is not None
        if self.rolling_schedule_settings.enabled and pagination_only:
            schedule_query = _paginated_schedule_query(request, question)
            if schedule_query.is_schedule_intent:
                emit(
                    {
                        "type": "status",
                        "stage": "rolling_schedule",
                        "status": "searching",
                    }
                )
                schedule_plan = self.rolling_schedule.prepare(
                    request_id,
                    question,
                    num_predict=request.num_predict,
                    query=schedule_query,
                    expected_snapshot_version=request.schedule_snapshot_version,
                    pagination_only=True,
                )
        elif self.rolling_schedule_settings.enabled and is_schedule_candidate(question):
            emit(
                {
                    "type": "status",
                    "stage": "rolling_schedule",
                    "status": "understanding",
                }
            )
            today = datetime.now(MOSCOW).date()
            analysis_content = None
            try:
                with self.gates.llm.acquire(cancel_event=cancel_event):
                    analysis_content = self.ollama.generate(
                        self.rolling_schedule.intent_request(question, today=today)
                    ).content
            except LLMError:
                LOGGER.warning(
                    "rolling schedule intent analysis failed; using deterministic parser",
                    exc_info=True,
                )
            schedule_query = self.rolling_schedule.resolve_query(
                question, today=today, analysis_content=analysis_content
            )
            if schedule_query is not None:
                emit(
                    {
                        "type": "status",
                        "stage": "rolling_schedule",
                        "status": "searching",
                    }
                )
                schedule_plan = self.rolling_schedule.prepare(
                    request_id,
                    question,
                    num_predict=request.num_predict,
                    query=schedule_query,
                    expected_snapshot_version=request.schedule_snapshot_version,
                    pagination_only=False,
                )
        if schedule_plan is not None:
            if schedule_plan.llm_request is None:
                fallback = schedule_plan.fallback
                fallback.latency.ttft_seconds = fallback.latency.total_seconds
                emit({"type": "delta", "delta": fallback.answer})
                return fallback

            first_token_seen = False

            def on_schedule_delta(delta: str) -> None:
                nonlocal first_token_seen
                if cancel_event.is_set():
                    raise StageCancelledError("llm", "llm request cancelled")
                if not first_token_seen:
                    first_token_seen = True
                    schedule_plan.fallback.latency.ttft_seconds = (
                        time.perf_counter() - schedule_plan.started
                    )
                # Buffer the short answer until the grounding check succeeds.
                # Streaming unverified text would make it impossible to retract
                # a hallucinated date, size or volume from the browser.

            emit({"type": "status", "stage": "llm", "status": "waiting"})
            with self.gates.llm.acquire(
                cancel_event=cancel_event,
                on_queued=lambda position: emit(
                    {
                        "type": "status",
                        "stage": "llm",
                        "status": "queued",
                        "queue_position": position,
                    }
                ),
            ) as lease:
                emit(
                    {
                        "type": "status",
                        "stage": "llm",
                        "status": "generating",
                        "queue_wait_seconds": round(lease.wait_seconds, 4),
                    }
                )
                started = time.perf_counter()
                try:
                    generated = self.ollama.stream(
                        schedule_plan.llm_request,
                        on_delta=on_schedule_delta,
                        cancel_event=cancel_event,
                    )
                    generation_seconds = time.perf_counter() - started
                except LLMError:
                    if first_token_seen:
                        raise
                    fallback = schedule_plan.fallback
                    fallback.latency.ttft_seconds = (
                        time.perf_counter() - schedule_plan.started
                    )
                    emit({"type": "delta", "delta": fallback.answer})
                    return fallback
            self._raise_if_cancelled(cancel_event, "llm")
            final = self.rolling_schedule.finish(
                schedule_plan,
                generated,
                llm_queue_wait_seconds=lease.wait_seconds,
                llm_queue_position=lease.initial_queue_position,
                generation_seconds=generation_seconds,
            )
            emit({"type": "delta", "delta": final.answer})
            return final
        prepared = self._prepare(
            request, emit=emit, cancel_event=cancel_event
        )
        first_token_seen = False

        def on_delta(delta: str) -> None:
            nonlocal first_token_seen
            if cancel_event.is_set():
                raise StageCancelledError("llm", "llm request cancelled")
            if not first_token_seen:
                first_token_seen = True
                prepared.latency.ttft_seconds = (
                    time.perf_counter() - prepared.total_started
                )
            emit({"type": "delta", "delta": delta})

        emit({"type": "status", "stage": "llm", "status": "waiting"})
        with self.gates.llm.acquire(
            cancel_event=cancel_event,
            on_queued=lambda position: emit(
                {
                    "type": "status",
                    "stage": "llm",
                    "status": "queued",
                    "queue_position": position,
                }
            ),
        ) as lease:
            prepared.latency.llm_queue_wait_seconds = lease.wait_seconds
            prepared.telemetry.llm_queue_position = lease.initial_queue_position
            emit(
                {
                    "type": "status",
                    "stage": "llm",
                    "status": "generating",
                    "queue_wait_seconds": round(lease.wait_seconds, 4),
                }
            )
            started = time.perf_counter()
            generated = self.generator.generate_stream(
                prepared.context,
                on_delta=on_delta,
                cancel_event=cancel_event,
                retrieval_context_seconds=self._retrieval_context_seconds(
                    prepared.latency
                ),
                num_predict=request.num_predict,
            )
            prepared.latency.generation_seconds = time.perf_counter() - started
        self._raise_if_cancelled(cancel_event, "llm")
        emit({"type": "status", "stage": "validation", "status": "validating"})
        return self._response(request_id, prepared, generated)

    def _prepare(
        self,
        request: AnswerRequest,
        *,
        emit: Callable[[dict[str, Any]], None],
        cancel_event: threading.Event | None,
    ) -> PreparedRequest:
        query = request.question.strip()
        if not query:
            raise ValueError("question cannot be empty")
        candidate_limit = request.candidate_limit or 40
        top_k = request.top_k or 8
        if top_k > candidate_limit:
            raise ValueError("top_k cannot exceed candidate_limit")
        latency = APILatency()
        telemetry = APITelemetry()
        telemetry.context_budget_tokens = (
            request.max_context_tokens
            or self.api_settings.default_max_context_tokens
        )
        telemetry.max_chunks_budget = (
            request.max_chunks or self.api_settings.default_max_chunks
        )
        telemetry.output_limit_tokens = (
            request.num_predict or self.generation_settings.num_predict
        )
        total_started = time.perf_counter()

        emit({"type": "status", "stage": "embedding", "status": "waiting"})
        with self.gates.embedding.acquire(
            cancel_event=cancel_event,
            on_queued=lambda position: emit(
                {
                    "type": "status",
                    "stage": "embedding",
                    "status": "queued",
                    "queue_position": position,
                }
            ),
        ) as lease:
            latency.embedding_queue_wait_seconds = lease.wait_seconds
            telemetry.embedding_queue_position = lease.initial_queue_position
            emit(
                {
                    "type": "status",
                    "stage": "embedding",
                    "status": "processing",
                    "queue_wait_seconds": round(lease.wait_seconds, 4),
                }
            )
            candidates, search_timings = self.hybrid.search_with_timings(
                query, mode="hybrid", top_k=candidate_limit
            )
            latency.embedding_execution_seconds = (
                search_timings.dense_embedding_seconds
            )
            latency.retrieval_seconds = (
                search_timings.retrieval_without_dense_embedding_seconds
            )
        telemetry.candidate_chunks = len(candidates)
        self._raise_if_cancelled(cancel_event, "embedding")

        emit({"type": "status", "stage": "reranker", "status": "waiting"})
        with self.gates.reranker.acquire(
            cancel_event=cancel_event,
            on_queued=lambda position: emit(
                {
                    "type": "status",
                    "stage": "reranker",
                    "status": "queued",
                    "queue_position": position,
                }
            ),
        ) as lease:
            latency.reranker_queue_wait_seconds = lease.wait_seconds
            telemetry.reranker_queue_position = lease.initial_queue_position
            emit(
                {
                    "type": "status",
                    "stage": "reranker",
                    "status": "processing",
                    "queue_wait_seconds": round(lease.wait_seconds, 4),
                }
            )
            started = time.perf_counter()
            reranked = self.rerank_service.rerank_candidates(
                query, candidates, top_k=top_k
            )
            reranker_seconds = time.perf_counter() - started
            latency.reranker_seconds = reranker_seconds
            latency.reranker_execution_seconds = reranker_seconds
        telemetry.reranked_chunks = len(reranked)
        self._raise_if_cancelled(cancel_event, "reranker")

        emit({"type": "status", "stage": "context", "status": "building"})
        started = time.perf_counter()
        context = ContextBuilder(
            self.chunks,
            self.assets,
            settings=ContextBuilderSettings(
                max_context_tokens=telemetry.context_budget_tokens,
                max_chunks=telemetry.max_chunks_budget,
                max_chunks_per_document=3,
                neighbor_expansion_limit=2,
            ),
        ).build(query, reranked)
        latency.context_builder_seconds = time.perf_counter() - started
        telemetry.context_chunks = len(context.sources)
        telemetry.context_sources = len(
            {source.document_id for source in context.sources}
        )
        telemetry.context_estimated_tokens = context.token_count
        telemetry.context_characters = len(context.rendered_context)
        telemetry.context_source_characters = sum(
            len(source.text) for source in context.sources
        )
        telemetry.context_render_overhead_characters = max(
            0,
            telemetry.context_characters - telemetry.context_source_characters,
        )
        telemetry.context_stats = {
            key: int(value)
            for key, value in context.stats.model_dump(mode="python").items()
        }
        self._raise_if_cancelled(cancel_event, "context")
        return PreparedRequest(
            context=context,
            latency=latency,
            telemetry=telemetry,
            total_started=total_started,
        )

    @staticmethod
    def _raise_if_cancelled(
        cancel_event: threading.Event | None, stage: str
    ) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise StageCancelledError(stage, f"{stage} request cancelled")

    @staticmethod
    def _retrieval_context_seconds(latency: APILatency) -> float:
        return (
            latency.embedding_execution_seconds
            + latency.retrieval_seconds
            + latency.reranker_execution_seconds
            + latency.context_builder_seconds
        )

    def _response(
        self,
        request_id: str,
        prepared: PreparedRequest,
        generated: RAGAnswer,
    ) -> AnswerResponse:
        generated = self.presentation.enrich_attachments(generated)
        latency = prepared.latency
        latency.queue_wait_seconds = (
            latency.embedding_queue_wait_seconds
            + latency.reranker_queue_wait_seconds
            + latency.llm_queue_wait_seconds
        )
        latency.total_seconds = time.perf_counter() - prepared.total_started
        telemetry = prepared.telemetry
        telemetry.prompt_estimated_tokens = generated.prompt_estimated_tokens
        telemetry.prompt_eval_count = generated.prompt_eval_count
        if generated.prompt_eval_count is not None:
            telemetry.prompt_estimate_error_tokens = (
                generated.prompt_eval_count - generated.prompt_estimated_tokens
            )
            if generated.prompt_eval_count > 0:
                telemetry.prompt_estimate_ratio = round(
                    generated.prompt_estimated_tokens / generated.prompt_eval_count,
                    4,
                )
        telemetry.prompt_eval_duration_seconds = (
            generated.prompt_eval_duration_seconds
        )
        telemetry.eval_count = generated.eval_count
        telemetry.eval_duration_seconds = generated.eval_duration_seconds
        telemetry.load_duration_seconds = generated.load_duration_seconds
        telemetry.total_duration_seconds = generated.total_duration_seconds
        telemetry.done_reason = generated.done_reason
        query_hash = hashlib.sha256(generated.query.encode("utf-8")).hexdigest()[:12]
        LOGGER.info(
            "request_id=%s query_hash=%s total=%.3f queue=%.3f "
            "embedding=%.3f retrieval=%.3f reranker=%.3f context=%.3f "
            "generation=%.3f ttft=%s context_tokens=%d prompt_estimate=%d "
            "prompt_eval=%s eval_count=%s sources=%d",
            request_id,
            query_hash,
            latency.total_seconds,
            latency.queue_wait_seconds,
            latency.embedding_execution_seconds,
            latency.retrieval_seconds,
            latency.reranker_execution_seconds,
            latency.context_builder_seconds,
            latency.generation_seconds,
            latency.ttft_seconds,
            telemetry.context_estimated_tokens,
            telemetry.prompt_estimated_tokens,
            telemetry.prompt_eval_count,
            telemetry.eval_count,
            telemetry.context_sources,
        )
        return AnswerResponse(
            request_id=request_id,
            answer=generated.answer,
            status=generated.status,
            citation_validation=generated.citation_validation,
            citations=generated.citations,
            sources=generated.used_sources,
            source_attachments=generated.source_attachments,
            related_images=generated.related_images,
            warnings=generated.warnings,
            latency=latency,
            telemetry=telemetry,
            model=generated.model,
            context_id=generated.context_id,
        )

    def gate_snapshots(self) -> dict[str, dict[str, Any]]:
        return {
            "embedding": self.gates.embedding.snapshot(),
            "reranker": self.gates.reranker.snapshot(),
            "llm": self.gates.llm.snapshot(),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ready = False
        try:
            self.ollama.close()
        finally:
            self.store.close()
        release = getattr(self.reranker, "release", None)
        if callable(release):
            release()
        release_dense = getattr(self.dense, "release", None)
        if callable(release_dense):
            release_dense()
