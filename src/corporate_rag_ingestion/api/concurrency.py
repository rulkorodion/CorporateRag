from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


class StageQueueError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


class StageQueueFullError(StageQueueError):
    pass


class StageQueueTimeoutError(StageQueueError):
    pass


class StageCancelledError(StageQueueError):
    pass


@dataclass(frozen=True)
class StageLease:
    stage: str
    wait_seconds: float
    initial_queue_position: int = 0


class StageGate:
    """Bounded FIFO concurrency gate for a synchronous heavy stage."""

    def __init__(self, name: str, *, limit: int, max_queue: int, timeout: float) -> None:
        if limit < 1 or max_queue < 0 or timeout <= 0:
            raise ValueError("invalid stage gate settings")
        self.name = name
        self.limit = limit
        self.max_queue = max_queue
        self.timeout = timeout
        self._condition = threading.Condition()
        self._active = 0
        self._queue: deque[str] = deque()
        self._max_observed_queue = 0
        self._total_acquired = 0
        self._total_rejected = 0
        self._total_timed_out = 0
        self._total_cancelled = 0

    @contextmanager
    def acquire(
        self,
        *,
        cancel_event: threading.Event | None = None,
        on_queued: Callable[[int], None] | None = None,
    ) -> Iterator[StageLease]:
        started = time.perf_counter()
        token: str | None = None
        initial_queue_position = 0
        last_reported_queue_position = 0
        with self._condition:
            if cancel_event is not None and cancel_event.is_set():
                self._total_cancelled += 1
                raise StageCancelledError(self.name, f"{self.name} request cancelled")
            if self._active < self.limit and not self._queue:
                self._active += 1
            else:
                if len(self._queue) >= self.max_queue:
                    self._total_rejected += 1
                    raise StageQueueFullError(
                        self.name,
                        f"{self.name} queue is full ({self.max_queue} waiting)",
                    )
                token = uuid.uuid4().hex
                self._queue.append(token)
                initial_queue_position = len(self._queue)
                last_reported_queue_position = initial_queue_position
                self._max_observed_queue = max(
                    self._max_observed_queue, len(self._queue)
                )
                if on_queued is not None:
                    on_queued(initial_queue_position)
                deadline = time.monotonic() + self.timeout
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        try:
                            self._queue.remove(token)
                        except ValueError:
                            pass
                        self._total_cancelled += 1
                        self._condition.notify_all()
                        raise StageCancelledError(
                            self.name, f"{self.name} request cancelled"
                        )
                    current_queue_position = self._queue.index(token) + 1
                    if current_queue_position != last_reported_queue_position:
                        last_reported_queue_position = current_queue_position
                        if on_queued is not None:
                            on_queued(current_queue_position)
                    is_first = bool(self._queue) and self._queue[0] == token
                    if is_first and self._active < self.limit:
                        self._queue.popleft()
                        self._active += 1
                        self._condition.notify_all()
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        try:
                            self._queue.remove(token)
                        except ValueError:
                            pass
                        self._total_timed_out += 1
                        self._condition.notify_all()
                        raise StageQueueTimeoutError(
                            self.name,
                            f"timed out after {self.timeout:g}s waiting for {self.name}",
                        )
                    self._condition.wait(min(remaining, 0.1))
            self._total_acquired += 1
        lease = StageLease(
            stage=self.name,
            wait_seconds=max(0.0, time.perf_counter() - started),
            initial_queue_position=initial_queue_position,
        )
        try:
            yield lease
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def snapshot(self) -> dict[str, int | float | str]:
        with self._condition:
            return {
                "stage": self.name,
                "limit": self.limit,
                "active": self._active,
                "queued": len(self._queue),
                "max_queue_size": self.max_queue,
                "max_observed_queue": self._max_observed_queue,
                "wait_timeout_seconds": self.timeout,
                "total_acquired": self._total_acquired,
                "total_rejected": self._total_rejected,
                "total_timed_out": self._total_timed_out,
                "total_cancelled": self._total_cancelled,
            }
