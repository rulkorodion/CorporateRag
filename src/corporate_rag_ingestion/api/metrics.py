from __future__ import annotations

import math
import threading
import time
from collections import deque


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 4)


class RuntimeMetrics:
    def __init__(self, *, window_size: int) -> None:
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._active_requests = 0
        self._completed = 0
        self._failed = 0
        self._latencies: deque[float] = deque(maxlen=window_size)

    def request_started(self) -> None:
        with self._lock:
            self._active_requests += 1

    def request_finished(self, latency: float, *, failed: bool) -> None:
        with self._lock:
            self._active_requests = max(0, self._active_requests - 1)
            if failed:
                self._failed += 1
            else:
                self._completed += 1
            self._latencies.append(latency)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            values = list(self._latencies)
            active = self._active_requests
            completed = self._completed
            failed = self._failed
        rss = None
        try:
            import psutil

            rss = int(psutil.Process().memory_info().rss)
        except ImportError:
            pass
        return {
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "active_requests": active,
            "completed_requests": completed,
            "failed_requests": failed,
            "latency_samples": len(values),
            "latency_p50_seconds": _percentile(values, 0.50),
            "latency_p95_seconds": _percentile(values, 0.95),
            "process_rss_bytes": rss,
        }

