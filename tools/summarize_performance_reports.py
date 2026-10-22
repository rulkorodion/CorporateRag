from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
from typing import Any


def value(item: dict[str, Any], *path: str) -> Any:
    current: Any = item
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize Stage 13 JSON reports")
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()
    print(
        "| mode | users | success | throughput | latency p50 | latency p95 | "
        "TTFT p50 | queue p95 | prompt eval avg | generation avg | output tokens avg | "
        "GPU0 MiB | GPU1 MiB |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    report_paths = [
        Path(match)
        for candidate in args.reports
        for match in (glob.glob(str(candidate)) or [str(candidate)])
    ]
    for path in report_paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        for level in report.get("levels", []):
            row = [
                report.get("mode"),
                level.get("users"),
                level.get("success_rate"),
                level.get("throughput_requests_per_second"),
                value(level, "latency_seconds", "p50"),
                value(level, "latency_seconds", "p95"),
                value(level, "ttft_seconds", "p50"),
                value(level, "queue_wait_seconds", "p95"),
                value(level, "prompt_eval_duration_seconds", "average"),
                value(level, "generation_seconds", "average"),
                value(level, "output_tokens", "average"),
                value(level, "resource_peaks", "gpu_peak_vram_mib", "0"),
                value(level, "resource_peaks", "gpu_peak_vram_mib", "1"),
            ]
            print("| " + " | ".join("" if item is None else str(item) for item in row) + " |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
