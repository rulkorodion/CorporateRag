from __future__ import annotations

from typing import Protocol

from ..chunking.tokens import TokenEstimator


class ContextTokenCounter(Protocol):
    method: str

    def count(self, text: str) -> int: ...


class StableTokenCounter:
    """Replaceable offline counter used until the final LLM tokenizer is selected."""

    method = TokenEstimator.method

    def __init__(self) -> None:
        self._estimator = TokenEstimator()

    def count(self, text: str) -> int:
        return self._estimator.count(text)
