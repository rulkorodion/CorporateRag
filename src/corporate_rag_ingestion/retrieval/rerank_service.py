from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from .models import SearchResult
from .reranker import RerankerBackend
from .settings import RetrievalSettings


class CandidateRetriever(Protocol):
    def search(
        self,
        query: str,
        *,
        mode: str,
        top_k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]: ...


class RerankerUnavailableError(RuntimeError):
    pass


class RerankSearchService:
    def __init__(
        self,
        settings: RetrievalSettings,
        candidates: CandidateRetriever,
        reranker: RerankerBackend,
        *,
        release_candidate_backend: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.candidates = candidates
        self.reranker = reranker
        self.release_candidate_backend = release_candidate_backend

    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        candidate_limit: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        final_limit = top_k or self.settings.reranker_final_top_k
        candidates_limit = candidate_limit or self.settings.reranker_candidate_limit
        if final_limit <= 0 or candidates_limit <= 0:
            raise ValueError("top_k and candidate_limit must be positive")
        candidates_limit = max(final_limit, candidates_limit)
        hybrid = self.candidates.search(
            query,
            mode="hybrid",
            top_k=candidates_limit,
            filters=filters,
        )
        if self.release_candidate_backend is not None:
            self.release_candidate_backend()
        return self.rerank_candidates(query, hybrid, top_k=final_limit)

    def rerank_candidates(
        self,
        query: str,
        candidates: Sequence[SearchResult],
        *,
        top_k: int,
    ) -> list[SearchResult]:
        if not candidates:
            return []
        documents = [self.document_text(candidate) for candidate in candidates]
        try:
            scores = self.reranker.score(query, documents)
        except Exception as exc:
            raise RerankerUnavailableError(f"reranker failed: {exc}") from exc
        if len(scores) != len(candidates):
            raise RerankerUnavailableError(
                f"reranker returned {len(scores)} scores for {len(candidates)} candidates"
            )
        ranked = sorted(
            zip(candidates, scores, strict=True),
            key=lambda item: (-float(item[1]), item[0].final_rank, item[0].chunk_id),
        )[:top_k]
        return [
            candidate.model_copy(
                update={
                    "mode": "rerank",
                    "final_rank": rank,
                    "final_score": float(score),
                    "reranker_score": float(score),
                    "rrf_rank": candidate.rrf_rank or candidate.final_rank,
                }
            )
            for rank, (candidate, score) in enumerate(ranked, start=1)
        ]

    def document_text(self, candidate: SearchResult) -> str:
        if not self.settings.reranker_include_headings:
            return candidate.text
        headings = list(candidate.heading_path)
        if candidate.section_title and candidate.section_title not in headings:
            headings.append(candidate.section_title)
        if not headings:
            return candidate.text
        return f"Section: {' > '.join(headings)}\n\n{candidate.text}"
