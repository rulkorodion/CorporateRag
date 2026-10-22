from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from ..generation.base import (
    LLMHTTPError,
    LLMCancelledError,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from ..retrieval.rerank_service import RerankerUnavailableError
from ..ui.files import mime_type
from .concurrency import (
    StageCancelledError,
    StageQueueFullError,
    StageQueueTimeoutError,
)
from .metrics import RuntimeMetrics
from .models import (
    AnswerRequest,
    AnswerResponse,
    ErrorResponse,
    HealthResponse,
)
from .runtime import APIAnswerRuntime, ProductionRAGRuntime
from .settings import APISettings
from .telemetry_store import TelemetryStore
from .viewer import (
    DocumentViewerService,
    OnlyOfficeConfigurationError,
    ViewerNotFoundError,
    ViewerUnsupportedError,
    ranged_file_response,
)


LOGGER = logging.getLogger(__name__)
RuntimeFactory = Callable[[APISettings], APIAnswerRuntime]


async def _retry_warmup_until_ready(
    runtime: APIAnswerRuntime,
    *,
    initial_delay_seconds: float,
    max_delay_seconds: float,
    stop_event: asyncio.Event,
) -> None:
    delay = initial_delay_seconds
    attempt = 1
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
            return
        except TimeoutError:
            pass

        attempt += 1
        try:
            await asyncio.to_thread(runtime.warmup)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning(
                "RAG runtime warmup retry %d failed (%s: %s); retrying in %.1fs",
                attempt,
                type(exc).__name__,
                exc,
                min(max_delay_seconds, delay * 2),
            )
            delay = min(max_delay_seconds, delay * 2)
            continue

        LOGGER.info("RAG runtime warmup recovered automatically on attempt %d", attempt)
        return


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", uuid.uuid4().hex)


def _error(
    request: Request,
    *,
    status_code: int,
    error: str,
    message: str,
    stage: str | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        request_id=_request_id(request), error=error, message=message, stage=stage
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


def _error_metadata(exc: Exception) -> tuple[str, str | None]:
    if isinstance(exc, StageQueueFullError):
        return "queue_full", exc.stage
    if isinstance(exc, StageQueueTimeoutError):
        return "queue_timeout", exc.stage
    if isinstance(exc, LLMTimeoutError):
        return "llm_timeout", "llm"
    if isinstance(exc, LLMUnavailableError):
        return "ollama_unavailable", "llm"
    if isinstance(exc, (LLMHTTPError, LLMResponseError)):
        return "llm_error", "llm"
    if isinstance(exc, RerankerUnavailableError):
        return "reranker_unavailable", "reranker"
    if isinstance(exc, ValueError):
        return "invalid_request", None
    return "pipeline_unavailable", None


def _record_success(
    store: TelemetryStore | None,
    response: AnswerResponse,
    *,
    started_at: datetime,
    endpoint: str,
    streaming: bool,
) -> None:
    if store is None:
        return
    try:
        store.record_success(
            response,
            started_at=started_at,
            endpoint=endpoint,
            streaming=streaming,
        )
    except Exception:
        LOGGER.exception(
            "request_id=%s telemetry success record failed", response.request_id
        )


def _record_failure(
    store: TelemetryStore | None,
    request_id: str,
    *,
    started_at: datetime,
    endpoint: str,
    streaming: bool,
    outcome: str,
    exc: Exception | None = None,
) -> None:
    if store is None:
        return
    error_code, error_stage = (
        _error_metadata(exc) if exc is not None else ("request_cancelled", None)
    )
    try:
        store.record_failure(
            request_id,
            started_at=started_at,
            endpoint=endpoint,
            streaming=streaming,
            outcome=outcome,
            error_code=error_code,
            error_stage=error_stage,
        )
    except Exception:
        LOGGER.exception("request_id=%s telemetry failure record failed", request_id)


def create_app(
    *,
    settings: APISettings | None = None,
    runtime_factory: RuntimeFactory | None = None,
) -> FastAPI:
    api_settings = settings or APISettings()
    factory = runtime_factory or (
        lambda value: ProductionRAGRuntime(api_settings=value)
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = factory(api_settings)
        warmup_retry_task: asyncio.Task[None] | None = None
        warmup_stop_event = asyncio.Event()
        app.state.runtime = runtime
        app.state.metrics = RuntimeMetrics(window_size=api_settings.metrics_window_size)
        app.state.telemetry_store = None
        if api_settings.telemetry_storage_enabled:
            telemetry_store = TelemetryStore(
                api_settings.telemetry_database_path,
                retention_days=api_settings.telemetry_retention_days,
            )
            try:
                await asyncio.to_thread(telemetry_store.initialize)
                app.state.telemetry_store = telemetry_store
            except Exception:
                LOGGER.exception(
                    "Persistent telemetry initialization failed; RAG continues without it"
                )
        if api_settings.warmup_on_startup:
            try:
                await asyncio.to_thread(runtime.warmup)
            except Exception:
                # Keep liveness available and recover without human action when
                # dependencies become reachable after a host or Docker restart.
                LOGGER.exception(
                    "RAG runtime warmup failed; automatic retries are starting"
                )
                warmup_retry_task = asyncio.create_task(
                    _retry_warmup_until_ready(
                        runtime,
                        initial_delay_seconds=(
                            api_settings.warmup_retry_initial_seconds
                        ),
                        max_delay_seconds=api_settings.warmup_retry_max_seconds,
                        stop_event=warmup_stop_event,
                    ),
                    name="rag-warmup-recovery",
                )
        try:
            yield
        finally:
            warmup_stop_event.set()
            if warmup_retry_task is not None:
                warmup_retry_task.cancel()
                await asyncio.gather(warmup_retry_task, return_exceptions=True)
            await asyncio.to_thread(runtime.close)

    app = FastAPI(
        title="Corporate RAG API",
        version="1.3.0-stage14-onlyoffice",
        lifespan=lifespan,
    )
    app.state.api_settings = api_settings
    if api_settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=api_settings.allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
            max_age=600,
        )

    @app.middleware("http")
    async def request_metrics(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        started = time.perf_counter()
        metrics: RuntimeMetrics = request.app.state.metrics
        metrics.request_started()
        failed = True
        deferred = False
        try:
            response = await call_next(request)
            failed = response.status_code >= 400
            response.headers["X-Request-ID"] = request.state.request_id
            if response.headers.get("content-type", "").startswith(
                "application/x-ndjson"
            ):
                original_iterator = response.body_iterator

                async def measured_stream():  # type: ignore[no-untyped-def]
                    stream_failed = failed
                    try:
                        async for chunk in original_iterator:
                            yield chunk
                    except BaseException:
                        stream_failed = True
                        raise
                    finally:
                        elapsed = time.perf_counter() - started
                        metrics.request_finished(elapsed, failed=stream_failed)
                        LOGGER.info(
                            "request_id=%s method=%s path=%s status=%s total=%.3f",
                            request.state.request_id,
                            request.method,
                            request.url.path,
                            response.status_code,
                            elapsed,
                        )

                response.body_iterator = measured_stream()
                deferred = True
                return response
            LOGGER.info(
                "request_id=%s method=%s path=%s status=%s total=%.3f",
                request.state.request_id,
                request.method,
                request.url.path,
                response.status_code,
                time.perf_counter() - started,
            )
            return response
        finally:
            if not deferred:
                metrics.request_finished(time.perf_counter() - started, failed=failed)

    @app.exception_handler(StageQueueFullError)
    async def queue_full(request: Request, exc: StageQueueFullError) -> JSONResponse:
        LOGGER.warning(
            "request_id=%s stage=%s queue_full", _request_id(request), exc.stage
        )
        return _error(
            request,
            status_code=429,
            error="queue_full",
            message=str(exc),
            stage=exc.stage,
        )

    @app.exception_handler(StageQueueTimeoutError)
    async def queue_timeout(request: Request, exc: StageQueueTimeoutError) -> JSONResponse:
        LOGGER.warning(
            "request_id=%s stage=%s queue_timeout", _request_id(request), exc.stage
        )
        return _error(
            request,
            status_code=503,
            error="queue_timeout",
            message=str(exc),
            stage=exc.stage,
        )

    @app.exception_handler(LLMTimeoutError)
    async def llm_timeout(request: Request, exc: LLMTimeoutError) -> JSONResponse:
        return _error(
            request,
            status_code=504,
            error="llm_timeout",
            message=str(exc),
            stage="llm",
        )

    @app.exception_handler(LLMUnavailableError)
    async def llm_unavailable(request: Request, exc: LLMUnavailableError) -> JSONResponse:
        return _error(
            request,
            status_code=503,
            error="ollama_unavailable",
            message=str(exc),
            stage="llm",
        )

    @app.exception_handler(LLMHTTPError)
    @app.exception_handler(LLMResponseError)
    async def llm_error(request: Request, exc: Exception) -> JSONResponse:
        return _error(
            request,
            status_code=502,
            error="llm_error",
            message=str(exc),
            stage="llm",
        )

    @app.exception_handler(RerankerUnavailableError)
    async def reranker_error(
        request: Request, exc: RerankerUnavailableError
    ) -> JSONResponse:
        return _error(
            request,
            status_code=503,
            error="reranker_unavailable",
            message=str(exc),
            stage="reranker",
        )

    @app.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError) -> JSONResponse:
        return _error(
            request, status_code=422, error="invalid_request", message=str(exc)
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception("request_id=%s unhandled API error", _request_id(request))
        return _error(
            request,
            status_code=503,
            error="pipeline_unavailable",
            message="База знаний временно недоступна. Повторите запрос позже.",
        )

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        runtime = request.app.state.runtime
        stages = runtime.gate_snapshots() if hasattr(runtime, "gate_snapshots") else {}
        return HealthResponse(
            status="ok",
            metrics=request.app.state.metrics.snapshot(),
            stages=stages,
        )

    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        checks: dict[str, Any] = await asyncio.to_thread(
            request.app.state.runtime.readiness
        )
        status = 200 if checks.get("ready") else 503
        return JSONResponse(status_code=status, content=checks)

    @app.get("/api/analytics/summary")
    async def analytics_summary(
        request: Request,
        days: int = Query(default=7, ge=1, le=365),
    ) -> JSONResponse:
        store: TelemetryStore | None = request.app.state.telemetry_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "telemetry_unavailable",
                    "message": "Persistent telemetry storage is not configured.",
                },
            )
        payload = await asyncio.to_thread(store.summary, days=days)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.post(
        "/api/answer",
        response_model=AnswerResponse,
        responses={429: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def answer(request: Request, payload: AnswerRequest) -> AnswerResponse:
        request_id = _request_id(request)
        started_at = datetime.now(UTC)
        store: TelemetryStore | None = request.app.state.telemetry_store
        try:
            response = await asyncio.to_thread(
                request.app.state.runtime.answer, request_id, payload
            )
        except asyncio.CancelledError:
            _record_failure(
                store,
                request_id,
                started_at=started_at,
                endpoint="/api/answer",
                streaming=False,
                outcome="cancelled",
            )
            raise
        except Exception as exc:
            await asyncio.to_thread(
                _record_failure,
                store,
                request_id,
                started_at=started_at,
                endpoint="/api/answer",
                streaming=False,
                outcome="failed",
                exc=exc,
            )
            raise
        await asyncio.to_thread(
            _record_success,
            store,
            response,
            started_at=started_at,
            endpoint="/api/answer",
            streaming=False,
        )
        return response

    @app.post("/api/answer/stream")
    async def answer_stream(
        request: Request, payload: AnswerRequest
    ) -> StreamingResponse:
        request_id = _request_id(request)
        started_at = datetime.now(UTC)
        store: TelemetryStore | None = request.app.state.telemetry_store
        loop = asyncio.get_running_loop()
        events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        cancelled = threading.Event()

        def emit(event: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(events.put_nowait, event)

        def error_event(exc: Exception) -> dict[str, Any]:
            if isinstance(exc, StageQueueFullError):
                return {
                    "type": "error",
                    "error": "queue_full",
                    "message": str(exc),
                    "stage": exc.stage,
                    "status_code": 429,
                }
            if isinstance(exc, StageQueueTimeoutError):
                return {
                    "type": "error",
                    "error": "queue_timeout",
                    "message": str(exc),
                    "stage": exc.stage,
                    "status_code": 503,
                }
            if isinstance(exc, LLMTimeoutError):
                return {
                    "type": "error",
                    "error": "llm_timeout",
                    "message": str(exc),
                    "stage": "llm",
                    "status_code": 504,
                }
            if isinstance(exc, LLMUnavailableError):
                return {
                    "type": "error",
                    "error": "ollama_unavailable",
                    "message": str(exc),
                    "stage": "llm",
                    "status_code": 503,
                }
            if isinstance(exc, RerankerUnavailableError):
                return {
                    "type": "error",
                    "error": "reranker_unavailable",
                    "message": str(exc),
                    "stage": "reranker",
                    "status_code": 503,
                }
            if isinstance(exc, ValueError):
                return {
                    "type": "error",
                    "error": "invalid_request",
                    "message": str(exc),
                    "status_code": 422,
                }
            if isinstance(exc, (LLMHTTPError, LLMResponseError)):
                return {
                    "type": "error",
                    "error": "llm_error",
                    "message": str(exc),
                    "stage": "llm",
                    "status_code": 502,
                }
            LOGGER.exception(
                "request_id=%s unhandled streaming API error",
                request_id,
                exc_info=exc,
            )
            return {
                "type": "error",
                "error": "pipeline_unavailable",
                "message": "База знаний временно недоступна. Повторите запрос позже.",
                "status_code": 503,
            }

        def produce() -> None:
            try:
                response = request.app.state.runtime.stream_answer(
                    request_id,
                    payload,
                    emit=emit,
                    cancel_event=cancelled,
                )
                if cancelled.is_set():
                    _record_failure(
                        store,
                        request_id,
                        started_at=started_at,
                        endpoint="/api/answer/stream",
                        streaming=True,
                        outcome="cancelled",
                    )
                else:
                    emit({"type": "final", "data": response.model_dump(mode="json")})
                    _record_success(
                        store,
                        response,
                        started_at=started_at,
                        endpoint="/api/answer/stream",
                        streaming=True,
                    )
            except (StageCancelledError, LLMCancelledError):
                if not cancelled.is_set():
                    emit(
                        {
                            "type": "error",
                            "error": "request_cancelled",
                            "message": "Запрос отменён.",
                            "status_code": 499,
                        }
                    )
                _record_failure(
                    store,
                    request_id,
                    started_at=started_at,
                    endpoint="/api/answer/stream",
                    streaming=True,
                    outcome="cancelled",
                )
            except Exception as exc:
                if not cancelled.is_set():
                    emit(error_event(exc))
                _record_failure(
                    store,
                    request_id,
                    started_at=started_at,
                    endpoint="/api/answer/stream",
                    streaming=True,
                    outcome="cancelled" if cancelled.is_set() else "failed",
                    exc=None if cancelled.is_set() else exc,
                )
            finally:
                loop.call_soon_threadsafe(events.put_nowait, None)

        async def watch_disconnect() -> None:
            while not cancelled.is_set():
                if await request.is_disconnected():
                    cancelled.set()
                    return
                await asyncio.sleep(0.1)

        async def stream_events() -> AsyncIterator[bytes]:
            producer = asyncio.create_task(asyncio.to_thread(produce))
            monitor = asyncio.create_task(watch_disconnect())
            initial = {
                "type": "status",
                "status": "accepted",
                "request_id": request_id,
            }
            yield (json.dumps(initial, ensure_ascii=False) + "\n").encode("utf-8")
            try:
                while True:
                    event = await events.get()
                    if event is None:
                        break
                    yield (json.dumps(event, ensure_ascii=False) + "\n").encode(
                        "utf-8"
                    )
            finally:
                cancelled.set()
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
                await producer

        return StreamingResponse(
            stream_events(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "X-Content-Type-Options": "nosniff",
                "X-Request-ID": request_id,
            },
        )

    def viewer(request: Request) -> DocumentViewerService:
        service = getattr(request.app.state, "document_viewer", None)
        if service is not None:
            return service
        project_settings = getattr(request.app.state.runtime, "project_settings", None)
        if project_settings is None:
            raise HTTPException(status_code=503, detail="Viewer storage is unavailable")
        service = DocumentViewerService(project_settings, api_settings)
        request.app.state.document_viewer = service
        return service

    def viewer_error(exc: Exception) -> HTTPException:
        if isinstance(exc, ViewerNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, ViewerUnsupportedError):
            return HTTPException(status_code=415, detail=str(exc))
        if isinstance(exc, OnlyOfficeConfigurationError):
            return HTTPException(status_code=503, detail=str(exc))
        raise exc

    def inline_file(path: Path) -> FileResponse:
        encoded_name = quote(path.name)
        return FileResponse(
            path,
            media_type=mime_type(path),
            headers={
                "Content-Disposition": f"inline; filename*=UTF-8''{encoded_name}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/viewer/documents/{document_id}/{version_id}/metadata")
    async def viewer_document_metadata(
        request: Request, document_id: str, version_id: str
    ) -> JSONResponse:
        try:
            service = viewer(request)
            item = await asyncio.to_thread(service.document, document_id, version_id)
            return JSONResponse(
                item.metadata(),
                headers={"Cache-Control": "no-store"},
            )
        except (ViewerNotFoundError, ViewerUnsupportedError) as exc:
            raise viewer_error(exc) from exc

    @app.get("/api/viewer/documents/{document_id}/{version_id}/content")
    async def viewer_document_content(
        request: Request, document_id: str, version_id: str
    ):
        try:
            item = await asyncio.to_thread(
                viewer(request).document, document_id, version_id
            )
            if item.kind == "video":
                return ranged_file_response(
                    request, item.source_path, mime_type(item.source_path)
                )
            return inline_file(item.source_path)
        except (ViewerNotFoundError, ViewerUnsupportedError) as exc:
            raise viewer_error(exc) from exc

    @app.get("/api/viewer/documents/{document_id}/{version_id}/onlyoffice-config")
    async def viewer_document_onlyoffice_config(
        request: Request, document_id: str, version_id: str
    ) -> JSONResponse:
        try:
            service = viewer(request)
            item = await asyncio.to_thread(service.document, document_id, version_id)
            payload = await asyncio.to_thread(service.onlyoffice_config, item)
            return JSONResponse(payload, headers={"Cache-Control": "no-store"})
        except (
            ViewerNotFoundError,
            ViewerUnsupportedError,
            OnlyOfficeConfigurationError,
        ) as exc:
            raise viewer_error(exc) from exc

    @app.get(
        "/api/viewer/assets/{document_id}/{version_id}/{asset_id}/metadata"
    )
    async def viewer_asset_metadata(
        request: Request, document_id: str, version_id: str, asset_id: str
    ) -> JSONResponse:
        try:
            item = await asyncio.to_thread(
                viewer(request).asset, document_id, version_id, asset_id
            )
            return JSONResponse(item.metadata(), headers={"Cache-Control": "no-store"})
        except ViewerNotFoundError as exc:
            raise viewer_error(exc) from exc

    @app.get(
        "/api/viewer/assets/{document_id}/{version_id}/{asset_id}/content"
    )
    async def viewer_asset_content(
        request: Request, document_id: str, version_id: str, asset_id: str
    ) -> FileResponse:
        try:
            item = await asyncio.to_thread(
                viewer(request).asset, document_id, version_id, asset_id
            )
            return inline_file(item.path)
        except ViewerNotFoundError as exc:
            raise viewer_error(exc) from exc

    return app


app = create_app()
