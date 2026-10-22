from __future__ import annotations

import math
import sqlite3
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .models import AnswerResponse


_METRIC_COLUMNS = {
    "total_seconds": "total_seconds",
    "ttft_seconds": "ttft_seconds",
    "queue_wait_seconds": "queue_wait_seconds",
    "generation_seconds": "generation_seconds",
    "embedding_seconds": "embedding_execution_seconds",
    "retrieval_seconds": "retrieval_seconds",
    "reranker_seconds": "reranker_execution_seconds",
    "context_builder_seconds": "context_builder_seconds",
}

_ANALYTICS_TIME_ZONE = ZoneInfo("Europe/Moscow")

_RECORD_COLUMNS = (
    "request_id",
    "started_at",
    "completed_at",
    "endpoint",
    "streaming",
    "outcome",
    "response_status",
    "citation_validation",
    "error_code",
    "error_stage",
    "model",
    "total_seconds",
    "ttft_seconds",
    "queue_wait_seconds",
    "embedding_queue_wait_seconds",
    "embedding_execution_seconds",
    "retrieval_seconds",
    "reranker_queue_wait_seconds",
    "reranker_execution_seconds",
    "context_builder_seconds",
    "llm_queue_wait_seconds",
    "generation_seconds",
    "prompt_eval_count",
    "prompt_eval_duration_seconds",
    "eval_count",
    "eval_duration_seconds",
    "load_duration_seconds",
    "ollama_total_duration_seconds",
    "candidate_chunks",
    "reranked_chunks",
    "context_chunks",
    "context_sources",
    "source_count",
    "citation_count",
    "warning_count",
    "context_estimated_tokens",
    "prompt_estimated_tokens",
    "output_limit_tokens",
    "done_reason",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds")


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _rounded(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _metric_summary(values: Iterable[float | int | None]) -> dict[str, float | None]:
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return {"average": None, "p50": None, "p95": None, "max": None}
    return {
        "average": _rounded(sum(numbers) / len(numbers)),
        "p50": _rounded(_percentile(numbers, 0.50)),
        "p95": _rounded(_percentile(numbers, 0.95)),
        "max": _rounded(max(numbers)),
    }


class TelemetryStore:
    """Small, privacy-preserving SQLite store for aggregate RAG telemetry.

    Questions, answers, source text and file names are deliberately absent from
    the schema. The store keeps only operational measurements and coarse result
    metadata required by the administrator dashboard.
    """

    def __init__(self, path: Path, *, retention_days: int = 90) -> None:
        self.path = Path(path)
        self.retention_days = retention_days
        self._lock = threading.RLock()
        self._last_cleanup = 0.0

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS request_telemetry (
                    request_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    streaming INTEGER NOT NULL CHECK (streaming IN (0, 1)),
                    outcome TEXT NOT NULL,
                    response_status TEXT,
                    citation_validation TEXT,
                    error_code TEXT,
                    error_stage TEXT,
                    model TEXT,
                    total_seconds REAL,
                    ttft_seconds REAL,
                    queue_wait_seconds REAL,
                    embedding_queue_wait_seconds REAL,
                    embedding_execution_seconds REAL,
                    retrieval_seconds REAL,
                    reranker_queue_wait_seconds REAL,
                    reranker_execution_seconds REAL,
                    context_builder_seconds REAL,
                    llm_queue_wait_seconds REAL,
                    generation_seconds REAL,
                    prompt_eval_count INTEGER,
                    prompt_eval_duration_seconds REAL,
                    eval_count INTEGER,
                    eval_duration_seconds REAL,
                    load_duration_seconds REAL,
                    ollama_total_duration_seconds REAL,
                    candidate_chunks INTEGER,
                    reranked_chunks INTEGER,
                    context_chunks INTEGER,
                    context_sources INTEGER,
                    source_count INTEGER,
                    citation_count INTEGER,
                    warning_count INTEGER,
                    context_estimated_tokens INTEGER,
                    prompt_estimated_tokens INTEGER,
                    output_limit_tokens INTEGER,
                    done_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_request_telemetry_completed_at
                    ON request_telemetry(completed_at);
                CREATE INDEX IF NOT EXISTS idx_request_telemetry_outcome
                    ON request_telemetry(outcome, completed_at);
                """
            )
            self._cleanup_locked(connection, force=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _cleanup_locked(
        self, connection: sqlite3.Connection, *, force: bool = False
    ) -> None:
        now = time.monotonic()
        if not force and now - self._last_cleanup < 24 * 60 * 60:
            return
        cutoff = _iso(_utc_now() - timedelta(days=self.retention_days))
        connection.execute(
            "DELETE FROM request_telemetry WHERE completed_at < ?", (cutoff,)
        )
        self._last_cleanup = now

    def record_success(
        self,
        response: AnswerResponse,
        *,
        started_at: datetime,
        endpoint: str,
        streaming: bool,
    ) -> None:
        latency = response.latency
        telemetry = response.telemetry
        values = {
            "request_id": response.request_id,
            "started_at": _iso(started_at),
            "completed_at": _iso(_utc_now()),
            "endpoint": endpoint,
            "streaming": int(streaming),
            "outcome": "completed",
            "response_status": response.status,
            "citation_validation": response.citation_validation,
            "error_code": None,
            "error_stage": None,
            "model": response.model,
            "total_seconds": latency.total_seconds,
            "ttft_seconds": latency.ttft_seconds,
            "queue_wait_seconds": latency.queue_wait_seconds,
            "embedding_queue_wait_seconds": latency.embedding_queue_wait_seconds,
            "embedding_execution_seconds": latency.embedding_execution_seconds,
            "retrieval_seconds": latency.retrieval_seconds,
            "reranker_queue_wait_seconds": latency.reranker_queue_wait_seconds,
            "reranker_execution_seconds": latency.reranker_execution_seconds,
            "context_builder_seconds": latency.context_builder_seconds,
            "llm_queue_wait_seconds": latency.llm_queue_wait_seconds,
            "generation_seconds": latency.generation_seconds,
            "prompt_eval_count": telemetry.prompt_eval_count,
            "prompt_eval_duration_seconds": telemetry.prompt_eval_duration_seconds,
            "eval_count": telemetry.eval_count,
            "eval_duration_seconds": telemetry.eval_duration_seconds,
            "load_duration_seconds": telemetry.load_duration_seconds,
            "ollama_total_duration_seconds": telemetry.total_duration_seconds,
            "candidate_chunks": telemetry.candidate_chunks,
            "reranked_chunks": telemetry.reranked_chunks,
            "context_chunks": telemetry.context_chunks,
            "context_sources": telemetry.context_sources,
            "source_count": len(response.sources),
            "citation_count": len(response.citations),
            "warning_count": len(response.warnings),
            "context_estimated_tokens": telemetry.context_estimated_tokens,
            "prompt_estimated_tokens": telemetry.prompt_estimated_tokens,
            "output_limit_tokens": telemetry.output_limit_tokens,
            "done_reason": telemetry.done_reason,
        }
        self._insert(values)

    def record_failure(
        self,
        request_id: str,
        *,
        started_at: datetime,
        endpoint: str,
        streaming: bool,
        outcome: str,
        error_code: str,
        error_stage: str | None = None,
    ) -> None:
        completed_at = _utc_now()
        self._insert(
            {
                "request_id": request_id,
                "started_at": _iso(started_at),
                "completed_at": _iso(completed_at),
                "endpoint": endpoint,
                "streaming": int(streaming),
                "outcome": outcome,
                "response_status": None,
                "citation_validation": None,
                "error_code": error_code,
                "error_stage": error_stage,
                "model": None,
                "total_seconds": max(
                    0.0, (completed_at - started_at.astimezone(UTC)).total_seconds()
                ),
            }
        )

    def _insert(self, supplied: dict[str, Any]) -> None:
        values = {column: supplied.get(column) for column in _RECORD_COLUMNS}
        placeholders = ", ".join("?" for _ in _RECORD_COLUMNS)
        names = ", ".join(_RECORD_COLUMNS)
        assignments = ", ".join(
            f"{column}=excluded.{column}"
            for column in _RECORD_COLUMNS
            if column != "request_id"
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                f"INSERT INTO request_telemetry ({names}) VALUES ({placeholders}) "
                f"ON CONFLICT(request_id) DO UPDATE SET {assignments}",
                [values[column] for column in _RECORD_COLUMNS],
            )
            self._cleanup_locked(connection)

    def summary(self, *, days: int) -> dict[str, Any]:
        now = _utc_now()
        since = now - timedelta(days=days)
        with self._lock, self._connect() as connection:
            rows = list(
                connection.execute(
                    "SELECT * FROM request_telemetry "
                    "WHERE completed_at >= ? AND completed_at <= ? "
                    "ORDER BY completed_at ASC",
                    (_iso(since), _iso(now)),
                )
            )

        total = len(rows)
        completed = sum(row["outcome"] == "completed" for row in rows)
        failed = sum(row["outcome"] == "failed" for row in rows)
        cancelled = sum(row["outcome"] == "cancelled" for row in rows)
        with_sources = sum((row["source_count"] or 0) > 0 for row in rows)
        completed_rows = [row for row in rows if row["outcome"] == "completed"]

        latency = {
            name: _metric_summary(row[column] for row in completed_rows)
            for name, column in _METRIC_COLUMNS.items()
        }
        tokens = {
            "prompt_total": sum(row["prompt_eval_count"] or 0 for row in rows),
            "output_total": sum(row["eval_count"] or 0 for row in rows),
            "prompt_average": _rounded(
                sum(row["prompt_eval_count"] or 0 for row in rows) / completed
                if completed
                else None,
                1,
            ),
            "output_average": _rounded(
                sum(row["eval_count"] or 0 for row in rows) / completed
                if completed
                else None,
                1,
            ),
            "context_average": _rounded(
                sum(row["context_estimated_tokens"] or 0 for row in rows)
                / completed
                if completed
                else None,
                1,
            ),
        }

        citation_counts = Counter(
            row["citation_validation"]
            for row in rows
            if row["citation_validation"]
        )
        model_counts = Counter(row["model"] for row in rows if row["model"])
        return {
            "period": {
                "days": days,
                "from": _iso(since),
                "to": _iso(now),
            },
            "totals": {
                "requests": total,
                "completed": completed,
                "failed": failed,
                "cancelled": cancelled,
                "success_rate": round(completed / total, 4) if total else None,
                "with_sources": with_sources,
                "without_sources": completed - with_sources,
                "sources_total": sum(row["source_count"] or 0 for row in rows),
                "citations_total": sum(row["citation_count"] or 0 for row in rows),
                "warnings_total": sum(row["warning_count"] or 0 for row in rows),
            },
            "latency": latency,
            "tokens": tokens,
            "citation_validation": dict(citation_counts),
            "models": [
                {"model": model, "requests": count}
                for model, count in model_counts.most_common()
            ],
            "errors": [
                {
                    "error": row["error_code"],
                    "stage": row["error_stage"],
                    "occurred_at": row["completed_at"],
                }
                for row in reversed(rows)
                if row["error_code"]
            ],
            "series": self._series(rows, since=since, now=now, days=days),
            "privacy": {
                "stores_question_text": False,
                "stores_answer_text": False,
                "stores_source_text": False,
                "retention_days": self.retention_days,
            },
        }

    @staticmethod
    def _series(
        rows: list[sqlite3.Row], *, since: datetime, now: datetime, days: int
    ) -> list[dict[str, Any]]:
        hourly = days == 1
        local_since = since.astimezone(_ANALYTICS_TIME_ZONE)
        local_now = now.astimezone(_ANALYTICS_TIME_ZONE)
        cursor = local_since.replace(minute=0, second=0, microsecond=0) if hourly else local_since.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        step = timedelta(hours=1) if hourly else timedelta(days=1)
        buckets: dict[str, dict[str, Any]] = {}
        while cursor <= local_now:
            key = cursor.strftime("%Y-%m-%dT%H:00") if hourly else cursor.date().isoformat()
            buckets[key] = {
                "bucket": cursor.isoformat(),
                "requests": 0,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
            }
            cursor += step
        for row in rows:
            completed_at = datetime.fromisoformat(row["completed_at"]).astimezone(
                _ANALYTICS_TIME_ZONE
            )
            key = (
                completed_at.strftime("%Y-%m-%dT%H:00")
                if hourly
                else completed_at.date().isoformat()
            )
            bucket = buckets.get(key)
            if bucket is None:
                continue
            bucket["requests"] += 1
            outcome = row["outcome"]
            if outcome in {"completed", "failed", "cancelled"}:
                bucket[outcome] += 1
        return list(buckets.values())
