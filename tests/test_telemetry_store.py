from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from corporate_rag_ingestion.api.models import (
    APILatency,
    APITelemetry,
    AnswerResponse,
)
from corporate_rag_ingestion.api.telemetry_store import TelemetryStore


def response(request_id: str, *, total: float, ttft: float) -> AnswerResponse:
    return AnswerResponse(
        request_id=request_id,
        answer="Секретный текст ответа [1].",
        status="completed",
        citation_validation="valid",
        latency=APILatency(
            embedding_execution_seconds=0.1,
            retrieval_seconds=0.2,
            reranker_execution_seconds=1.5,
            context_builder_seconds=0.02,
            llm_queue_wait_seconds=0.4,
            queue_wait_seconds=0.4,
            generation_seconds=total - 2.22,
            ttft_seconds=ttft,
            total_seconds=total,
        ),
        telemetry=APITelemetry(
            candidate_chunks=40,
            reranked_chunks=8,
            context_chunks=6,
            context_sources=3,
            context_estimated_tokens=3100,
            prompt_estimated_tokens=3500,
            prompt_eval_count=3600,
            prompt_eval_duration_seconds=0.5,
            eval_count=180,
            eval_duration_seconds=4.0,
            load_duration_seconds=0.1,
            total_duration_seconds=6.0,
            output_limit_tokens=1024,
            done_reason="stop",
        ),
        model="test-model",
        context_id="test-context",
    )


def test_store_records_metrics_and_builds_summary(tmp_path) -> None:
    store = TelemetryStore(tmp_path / "telemetry" / "requests.db", retention_days=90)
    store.initialize()
    now = datetime.now(UTC)
    store.record_success(
        response("request-1", total=8.0, ttft=3.0),
        started_at=now - timedelta(seconds=8),
        endpoint="/api/answer/stream",
        streaming=True,
    )
    store.record_success(
        response("request-2", total=12.0, ttft=5.0),
        started_at=now - timedelta(seconds=12),
        endpoint="/api/answer/stream",
        streaming=True,
    )
    store.record_failure(
        "request-3",
        started_at=now - timedelta(seconds=2),
        endpoint="/api/answer/stream",
        streaming=True,
        outcome="failed",
        error_code="queue_timeout",
        error_stage="llm",
    )

    summary = store.summary(days=7)

    assert summary["totals"]["requests"] == 3
    assert summary["totals"]["completed"] == 2
    assert summary["totals"]["failed"] == 1
    assert summary["totals"]["success_rate"] == 0.6667
    assert summary["latency"]["total_seconds"]["average"] == 10.0
    assert summary["latency"]["ttft_seconds"]["p50"] == 4.0
    assert summary["tokens"]["prompt_total"] == 7200
    assert summary["tokens"]["output_total"] == 360
    assert summary["errors"][0]["error"] == "queue_timeout"
    assert summary["errors"][0]["occurred_at"]
    assert "count" not in summary["errors"][0]
    assert summary["privacy"]["stores_question_text"] is False


def test_store_schema_never_persists_question_answer_or_source_text(tmp_path) -> None:
    path = tmp_path / "requests.db"
    store = TelemetryStore(path)
    store.initialize()
    store.record_success(
        response("private-request", total=8.0, ttft=3.0),
        started_at=datetime.now(UTC) - timedelta(seconds=8),
        endpoint="/api/answer",
        streaming=False,
    )

    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(request_telemetry)")
        }
        values = list(connection.execute("SELECT * FROM request_telemetry"))[0]

    assert "question" not in columns
    assert "answer" not in columns
    assert "source_text" not in columns
    assert "Секретный текст ответа" not in " ".join(
        str(value) for value in values if value is not None
    )


def test_store_upserts_same_request_without_double_counting(tmp_path) -> None:
    store = TelemetryStore(tmp_path / "requests.db")
    store.initialize()
    started = datetime.now(UTC) - timedelta(seconds=5)
    store.record_success(
        response("same-request", total=5.0, ttft=2.0),
        started_at=started,
        endpoint="/api/answer",
        streaming=False,
    )
    store.record_success(
        response("same-request", total=7.0, ttft=3.0),
        started_at=started,
        endpoint="/api/answer",
        streaming=False,
    )

    summary = store.summary(days=1)
    assert summary["totals"]["requests"] == 1
    assert summary["latency"]["total_seconds"]["average"] == 7.0


def test_activity_series_uses_moscow_time() -> None:
    since = datetime(2026, 9, 10, 20, 30, tzinfo=UTC)
    now = datetime(2026, 9, 11, 1, 30, tzinfo=UTC)

    series = TelemetryStore._series([], since=since, now=now, days=1)

    assert series[0]["bucket"] == "2026-09-10T23:00:00+03:00"
    assert series[-1]["bucket"] == "2026-09-11T04:00:00+03:00"
    assert all(
        datetime.fromisoformat(item["bucket"]).utcoffset() == timedelta(hours=3)
        for item in series
    )
