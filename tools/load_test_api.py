from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx

try:
    import psutil
except ImportError:  # pragma: no cover - production API image includes psutil
    psutil = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = PROJECT_ROOT / "storage" / "reports" / "api-load-test.json"
QUESTIONS = [
    "Какая максимальная загрузка автомобиля?",
    "На какие трубы распространяется ТУ 24.20.13-004-40198874-2020?",
    "Кто подаёт заявление на открытие отдельного счёта по ГОЗ?",
    "Как настроить корпоративную подпись в Outlook?",
    "К кому обращаться, чтобы заказать визитку?",
    "Какое отклонение допускается при высоте профиля более 100 мм?",
    "Как создать заявку на отгрузку автомобиля?",
    "Сканирование ладони на сканере Biosmart",
    "Какие трубы мы производим?",
    "Какой пароль Wi-Fi в переговорной директора?",
]


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 4)


def numeric_summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"average": None, "p50": None, "p95": None, "max": None}
    return {
        "average": round(statistics.fmean(values), 4),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": round(max(values), 4),
    }


def metric_values(results: list[dict[str, Any]], key: str) -> list[float]:
    return [float(item[key]) for item in results if item.get(key) is not None]


class ResourceSampler:
    def __init__(self, client: httpx.AsyncClient, interval: float) -> None:
        self.client = client
        self.interval = interval
        self.stop = asyncio.Event()
        self.gpu_peak_vram_mib: dict[str, float] = {}
        self.gpu_peak_utilization_percent: dict[str, float] = {}
        self.system_ram_peak_bytes = 0
        self.api_rss_peak_bytes = 0
        self.samples = 0

    async def run(self) -> None:
        while not self.stop.is_set():
            await asyncio.gather(self._sample_gpu(), self._sample_health())
            if psutil is not None:
                self.system_ram_peak_bytes = max(
                    self.system_ram_peak_bytes, int(psutil.virtual_memory().used)
                )
            self.samples += 1
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=self.interval)

    async def _sample_gpu(self) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                "--query-gpu=index,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
            if process.returncode != 0:
                return
            for line in stdout.decode(errors="replace").splitlines():
                index, memory, utilization = (part.strip() for part in line.split(","))
                self.gpu_peak_vram_mib[index] = max(
                    self.gpu_peak_vram_mib.get(index, 0.0), float(memory)
                )
                self.gpu_peak_utilization_percent[index] = max(
                    self.gpu_peak_utilization_percent.get(index, 0.0),
                    float(utilization),
                )
        except (FileNotFoundError, ValueError, asyncio.TimeoutError):
            return

    async def _sample_health(self) -> None:
        try:
            response = await self.client.get("/health")
            response.raise_for_status()
            rss = int(response.json().get("metrics", {}).get("process_rss_bytes") or 0)
            self.api_rss_peak_bytes = max(self.api_rss_peak_bytes, rss)
        except Exception:
            return

    def snapshot(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "gpu_peak_vram_mib": self.gpu_peak_vram_mib,
            "gpu_peak_utilization_percent": self.gpu_peak_utilization_percent,
            "system_ram_peak_bytes": self.system_ram_peak_bytes or None,
            "api_process_rss_peak_bytes": self.api_rss_peak_bytes or None,
        }


async def one_stream(
    client: httpx.AsyncClient,
    question: str,
    number: int,
    *,
    max_context_tokens: int | None,
    num_predict: int | None,
    include_answer_text: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    first_delta_at: float | None = None
    final: dict[str, Any] | None = None
    stream_error: dict[str, Any] | None = None
    status_code: int | None = None
    payload: dict[str, Any] = {"question": question}
    if max_context_tokens is not None:
        payload["max_context_tokens"] = max_context_tokens
    if num_predict is not None:
        payload["num_predict"] = num_predict
    try:
        async with client.stream(
            "POST",
            "/api/answer/stream",
            headers={
                "X-Request-ID": f"load-{number:04d}",
                "Accept": "application/x-ndjson",
            },
            json=payload,
        ) as response:
            status_code = response.status_code
            if response.status_code >= 400:
                raw = await response.aread()
                return {
                    "request": number,
                    "question": question,
                    "status_code": response.status_code,
                    "success": False,
                    "client_seconds": round(time.perf_counter() - started, 4),
                    "error": raw.decode(errors="replace")[:500],
                }
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("type") == "delta" and first_delta_at is None:
                    first_delta_at = time.perf_counter()
                elif event.get("type") == "final":
                    final = event.get("data")
                elif event.get("type") == "error":
                    stream_error = event
    except Exception as exc:
        return {
            "request": number,
            "question": question,
            "status_code": status_code,
            "success": False,
            "client_seconds": round(time.perf_counter() - started, 4),
            "error": f"{type(exc).__name__}: {exc}",
        }

    elapsed = time.perf_counter() - started
    if final is None:
        return {
            "request": number,
            "question": question,
            "status_code": status_code,
            "success": False,
            "client_seconds": round(elapsed, 4),
            "ttft_seconds": round(first_delta_at - started, 4) if first_delta_at else None,
            "error": (stream_error or {}).get("error") or "missing_final_event",
            "error_detail": stream_error,
        }

    latency = final.get("latency") or {}
    telemetry = final.get("telemetry") or {}
    result = {
        "request": number,
        "question": question,
        "status_code": status_code,
        "success": True,
        "client_seconds": round(elapsed, 4),
        "ttft_seconds": round(first_delta_at - started, 4) if first_delta_at else None,
        "server_ttft_seconds": latency.get("ttft_seconds"),
        "server_seconds": latency.get("total_seconds"),
        "queue_wait_seconds": latency.get("queue_wait_seconds"),
        "embedding_queue_wait_seconds": latency.get("embedding_queue_wait_seconds"),
        "reranker_queue_wait_seconds": latency.get("reranker_queue_wait_seconds"),
        "llm_queue_wait_seconds": latency.get("llm_queue_wait_seconds"),
        "embedding_execution_seconds": latency.get("embedding_execution_seconds"),
        "retrieval_seconds": latency.get("retrieval_seconds"),
        "reranker_execution_seconds": latency.get("reranker_execution_seconds"),
        "context_builder_seconds": latency.get("context_builder_seconds"),
        "generation_seconds": latency.get("generation_seconds"),
        "prompt_eval_duration_seconds": telemetry.get("prompt_eval_duration_seconds"),
        "eval_duration_seconds": telemetry.get("eval_duration_seconds"),
        "load_duration_seconds": telemetry.get("load_duration_seconds"),
        "ollama_total_duration_seconds": telemetry.get("total_duration_seconds"),
        "prompt_eval_count": telemetry.get("prompt_eval_count"),
        "eval_count": telemetry.get("eval_count"),
        "context_estimated_tokens": telemetry.get("context_estimated_tokens"),
        "context_budget_tokens": telemetry.get("context_budget_tokens"),
        "output_limit_tokens": telemetry.get("output_limit_tokens"),
        "prompt_estimated_tokens": telemetry.get("prompt_estimated_tokens"),
        "prompt_estimate_ratio": telemetry.get("prompt_estimate_ratio"),
        "context_characters": telemetry.get("context_characters"),
        "context_render_overhead_characters": telemetry.get(
            "context_render_overhead_characters"
        ),
        "candidate_chunks": telemetry.get("candidate_chunks"),
        "reranked_chunks": telemetry.get("reranked_chunks"),
        "context_chunks": telemetry.get("context_chunks"),
        "context_sources": telemetry.get("context_sources"),
        "done_reason": telemetry.get("done_reason"),
        "citation_validation": final.get("citation_validation"),
        "request_id": final.get("request_id"),
    }
    if include_answer_text:
        result["answer"] = final.get("answer")
        result["source_paths"] = [
            source.get("relative_path") for source in final.get("sources") or []
        ]
    return result


SUMMARY_FIELDS = {
    "latency_seconds": "client_seconds",
    "ttft_seconds": "ttft_seconds",
    "queue_wait_seconds": "queue_wait_seconds",
    "embedding_queue_wait_seconds": "embedding_queue_wait_seconds",
    "reranker_queue_wait_seconds": "reranker_queue_wait_seconds",
    "llm_queue_wait_seconds": "llm_queue_wait_seconds",
    "embedding_execution_seconds": "embedding_execution_seconds",
    "retrieval_seconds": "retrieval_seconds",
    "reranker_execution_seconds": "reranker_execution_seconds",
    "context_builder_seconds": "context_builder_seconds",
    "generation_seconds": "generation_seconds",
    "prompt_eval_duration_seconds": "prompt_eval_duration_seconds",
    "eval_duration_seconds": "eval_duration_seconds",
    "prompt_tokens": "prompt_eval_count",
    "output_tokens": "eval_count",
    "context_estimated_tokens": "context_estimated_tokens",
    "prompt_estimate_ratio": "prompt_estimate_ratio",
}


async def run_level(
    client: httpx.AsyncClient,
    concurrency: int,
    requests: int,
    *,
    sample_interval: float,
    max_context_tokens: int | None,
    num_predict: int | None,
    include_answer_text: bool,
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(concurrency)
    sampler = ResourceSampler(client, sample_interval)
    sampler_task = asyncio.create_task(sampler.run())

    async def limited(index: int) -> dict[str, Any]:
        async with semaphore:
            return await one_stream(
                client,
                QUESTIONS[index % len(QUESTIONS)],
                index + 1,
                max_context_tokens=max_context_tokens,
                num_predict=num_predict,
                include_answer_text=include_answer_text,
            )

    started = time.perf_counter()
    try:
        results = await asyncio.gather(*(limited(index) for index in range(requests)))
    finally:
        sampler.stop.set()
        await sampler_task
    wall = time.perf_counter() - started
    success = sum(bool(item["success"]) for item in results)
    level: dict[str, Any] = {
        "users": concurrency,
        "requests": requests,
        "success": success,
        "errors": requests - success,
        "success_rate": round(success / requests, 4),
        "wall_seconds": round(wall, 4),
        "throughput_requests_per_second": round(success / wall, 4),
        "citation_validation": {
            value: sum(item.get("citation_validation") == value for item in results)
            for value in ["valid", "warning", "invalid"]
        },
        "done_reason": {
            value: sum(item.get("done_reason") == value for item in results)
            for value in sorted(
                {str(item.get("done_reason")) for item in results if item.get("done_reason")}
            )
        },
        "resource_peaks": sampler.snapshot(),
        "results": results,
    }
    for label, field in SUMMARY_FIELDS.items():
        level[label] = numeric_summary(metric_values(results, field))
    return level


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": args.mode,
        "base_url": args.base_url,
        "requests_per_level": args.requests,
        "max_context_tokens": args.max_context_tokens,
        "num_predict": args.num_predict,
        "levels": [],
    }
    timeout = httpx.Timeout(args.timeout)
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=timeout, trust_env=False
    ) as client:
        ready = await client.get("/ready")
        if ready.status_code != 200 or not ready.json().get("ready"):
            raise RuntimeError(f"API is not ready: {ready.status_code} {ready.text}")
        report["ready"] = ready.json()
        report["health_before"] = (await client.get("/health")).json()
        for users in args.users:
            level = await run_level(
                client,
                users,
                args.requests,
                sample_interval=args.sample_interval,
                max_context_tokens=args.max_context_tokens,
                num_predict=args.num_predict,
                include_answer_text=args.include_answer_text,
            )
            level["health_after"] = (await client.get("/health")).json()
            report["levels"].append(level)
            print(
                json.dumps(
                    {key: value for key, value in level.items() if key != "results"},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Streaming load test with latency, TTFT, queue and GPU telemetry"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--mode", default="unspecified")
    parser.add_argument("--users", type=int, nargs="+", default=[1, 2, 5, 10])
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--sample-interval", type=float, default=0.25)
    parser.add_argument("--max-context-tokens", type=int)
    parser.add_argument("--num-predict", type=int)
    parser.add_argument(
        "--include-answer-text",
        action="store_true",
        help="store generated answers for a controlled quality review",
    )
    parser.add_argument("--output", default=str(DEFAULT_REPORT))
    args = parser.parse_args()
    if args.requests < max(args.users):
        parser.error("--requests must be at least the largest --users value")
    if args.max_context_tokens is not None and args.num_predict is not None:
        parser.error(
            "change only one variable per run: context budget or output budget"
        )
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
