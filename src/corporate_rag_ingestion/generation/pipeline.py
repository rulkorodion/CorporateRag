from __future__ import annotations

import time
from typing import Any, Protocol

from ..context.builder import ContextBuilder
from ..context.models import ContextPackage
from ..retrieval.models import SearchResult
from .models import RAGAnswer
from .service import RAGAnswerService


class ContextProvider(Protocol):
    def build(self, query: str) -> ContextPackage: ...


class RerankedSearch(Protocol):
    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        candidate_limit: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]: ...


class RetrievalContextProvider:
    def __init__(
        self,
        retrieval: RerankedSearch,
        builder: ContextBuilder,
        *,
        top_k: int,
        candidate_limit: int,
        filters: dict[str, Any] | None = None,
    ) -> None:
        self.retrieval = retrieval
        self.builder = builder
        self.top_k = top_k
        self.candidate_limit = candidate_limit
        self.filters = filters

    def build(self, query: str) -> ContextPackage:
        results = self.retrieval.search(
            query,
            top_k=self.top_k,
            candidate_limit=self.candidate_limit,
            filters=self.filters,
        )
        return self.builder.build(query, results)


class RAGPipeline:
    def __init__(self, context_provider: ContextProvider, generator: RAGAnswerService) -> None:
        self.context_provider = context_provider
        self.generator = generator

    def answer(self, query: str) -> RAGAnswer:
        answer, _ = self.answer_with_context(query)
        return answer

    def answer_with_context(self, query: str) -> tuple[RAGAnswer, ContextPackage]:
        started = time.perf_counter()
        context = self.context_provider.build(query)
        context_seconds = time.perf_counter() - started
        return (
            self.generator.generate(
                context, retrieval_context_seconds=context_seconds
            ),
            context,
        )
