from __future__ import annotations

from collections.abc import Callable
from threading import Event
from typing import Protocol

from .models import LLMGenerationRequest, LLMGenerationResult


class LLMBackend(Protocol):
    def generate(self, request: LLMGenerationRequest) -> LLMGenerationResult: ...


class StreamingLLMBackend(LLMBackend, Protocol):
    def stream(
        self,
        request: LLMGenerationRequest,
        *,
        on_delta: Callable[[str], None],
        cancel_event: Event | None = None,
    ) -> LLMGenerationResult: ...


class LLMError(RuntimeError):
    """Base error exposed by an LLM backend."""


class LLMTimeoutError(LLMError):
    pass


class LLMUnavailableError(LLMError):
    pass


class LLMHTTPError(LLMError):
    pass


class LLMResponseError(LLMError):
    pass


class LLMCancelledError(LLMError):
    pass
