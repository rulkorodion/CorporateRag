from __future__ import annotations

import json
from collections.abc import Callable
from threading import Event

import httpx
from pydantic import ValidationError

from .base import (
    LLMHTTPError,
    LLMCancelledError,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from .models import (
    LLMGenerationRequest,
    LLMGenerationResult,
    OllamaChatResponse,
)
from .settings import GenerationSettings


class OllamaClient:
    def __init__(
        self,
        settings: GenerationSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.base_url,
            timeout=httpx.Timeout(settings.timeout_seconds),
            transport=transport,
            trust_env=settings.trust_env,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _payload(self, request: LLMGenerationRequest, *, stream: bool) -> dict[str, object]:
        options: dict[str, object] = {
            "temperature": self.settings.temperature,
            "num_ctx": self.settings.num_ctx,
        }
        num_predict = request.num_predict or self.settings.num_predict
        if num_predict is not None:
            options["num_predict"] = num_predict
        payload: dict[str, object] = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "think": self.settings.think,
            "stream": stream,
            "keep_alive": self.settings.keep_alive,
            "options": options,
        }
        if request.response_format is not None:
            payload["format"] = request.response_format
        return payload

    @staticmethod
    def _result(parsed: OllamaChatResponse, content: str) -> LLMGenerationResult:
        nanoseconds = 1_000_000_000
        return LLMGenerationResult(
            content=content.strip(),
            model=parsed.model,
            done_reason=parsed.done_reason,
            total_duration_seconds=(
                parsed.total_duration / nanoseconds
                if parsed.total_duration is not None
                else None
            ),
            load_duration_seconds=(
                parsed.load_duration / nanoseconds
                if parsed.load_duration is not None
                else None
            ),
            prompt_eval_count=parsed.prompt_eval_count,
            prompt_eval_duration_seconds=(
                parsed.prompt_eval_duration / nanoseconds
                if parsed.prompt_eval_duration is not None
                else None
            ),
            eval_count=parsed.eval_count,
            eval_duration_seconds=(
                parsed.eval_duration / nanoseconds
                if parsed.eval_duration is not None
                else None
            ),
        )

    def generate(self, request: LLMGenerationRequest) -> LLMGenerationResult:
        payload = self._payload(request, stream=False)
        try:
            response = self._client.post("/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"Ollama request exceeded {self.settings.timeout_seconds:g} seconds"
            ) from exc
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            raise LLMUnavailableError(
                f"Ollama is unavailable at {self.settings.base_url}: {exc}"
            ) from exc
        if response.status_code >= 400:
            body = response.text[:1000]
            raise LLMHTTPError(
                f"Ollama HTTP {response.status_code} at {self.settings.base_url}: {body}"
            )
        try:
            raw = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMResponseError("Ollama returned invalid JSON") from exc
        try:
            parsed = OllamaChatResponse.model_validate(raw)
        except ValidationError as exc:
            raise LLMResponseError(f"Ollama response schema is invalid: {exc}") from exc
        content = parsed.message.content.strip()
        if not content:
            raise LLMResponseError("Ollama returned an empty assistant message")
        return self._result(parsed, content)

    def stream(
        self,
        request: LLMGenerationRequest,
        *,
        on_delta: Callable[[str], None],
        cancel_event: Event | None = None,
    ) -> LLMGenerationResult:
        payload = self._payload(request, stream=True)
        content_parts: list[str] = []
        final: OllamaChatResponse | None = None
        try:
            with self._client.stream("POST", "/api/chat", json=payload) as response:
                if response.status_code >= 400:
                    body = response.read().decode(errors="replace")[:1000]
                    raise LLMHTTPError(
                        f"Ollama HTTP {response.status_code} at {self.settings.base_url}: {body}"
                    )
                for line in response.iter_lines():
                    if cancel_event is not None and cancel_event.is_set():
                        raise LLMCancelledError("Ollama generation cancelled")
                    if not line.strip():
                        continue
                    try:
                        parsed = OllamaChatResponse.model_validate_json(line)
                    except (ValidationError, ValueError) as exc:
                        raise LLMResponseError(
                            f"Ollama stream response is invalid: {exc}"
                        ) from exc
                    delta = parsed.message.content
                    if delta:
                        content_parts.append(delta)
                        on_delta(delta)
                    if parsed.done:
                        final = parsed
        except LLMCancelledError:
            raise
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"Ollama request exceeded {self.settings.timeout_seconds:g} seconds"
            ) from exc
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            raise LLMUnavailableError(
                f"Ollama is unavailable at {self.settings.base_url}: {exc}"
            ) from exc
        content = "".join(content_parts).strip()
        if final is None:
            raise LLMResponseError("Ollama stream ended without a final event")
        if not content:
            raise LLMResponseError("Ollama returned an empty assistant message")
        return self._result(final, content)
