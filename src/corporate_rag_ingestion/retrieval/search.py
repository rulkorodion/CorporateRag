from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

from qdrant_client import models

from .dense import DenseEmbeddingBackend
from .models import SearchResult
from .settings import RetrievalSettings
from .sparse import SparseEmbeddingBackend
from .store import QdrantStore


SearchMode = Literal["dense", "sparse", "hybrid"]


@dataclass(frozen=True)
class SearchTimings:
    total_seconds: float = 0.0
    dense_embedding_seconds: float = 0.0
    sparse_embedding_seconds: float = 0.0
    dense_query_seconds: float = 0.0
    sparse_query_seconds: float = 0.0
    fusion_seconds: float = 0.0

    @property
    def retrieval_without_dense_embedding_seconds(self) -> float:
        return max(0.0, self.total_seconds - self.dense_embedding_seconds)


class HybridSearchService:
    def __init__(
        self,
        settings: RetrievalSettings,
        store: QdrantStore,
        dense: DenseEmbeddingBackend,
        sparse: SparseEmbeddingBackend,
    ) -> None:
        self.settings = settings
        self.store = store
        self.dense = dense
        self.sparse = sparse

    def search(
        self,
        query: str,
        *,
        mode: SearchMode = "hybrid",
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        results, _ = self.search_with_timings(
            query, mode=mode, top_k=top_k, filters=filters
        )
        return results

    def search_with_timings(
        self,
        query: str,
        *,
        mode: SearchMode = "hybrid",
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> tuple[list[SearchResult], SearchTimings]:
        if not query.strip():
            raise ValueError("search query cannot be empty")
        total_started = time.perf_counter()
        query_filter = self.build_filter(filters or {})
        candidate_limit = max(top_k, self.settings.candidate_limit)
        dense_points = []
        sparse_points = []
        dense_embedding_seconds = 0.0
        sparse_embedding_seconds = 0.0
        dense_query_seconds = 0.0
        sparse_query_seconds = 0.0
        if mode in {"dense", "hybrid"}:
            started = time.perf_counter()
            dense_vector = self.dense.embed_query(query)
            dense_embedding_seconds = time.perf_counter() - started
            started = time.perf_counter()
            dense_points = self.store.query_dense(
                dense_vector,
                limit=candidate_limit if mode == "hybrid" else top_k,
                query_filter=query_filter,
            )
            dense_query_seconds = time.perf_counter() - started
        if mode in {"sparse", "hybrid"}:
            started = time.perf_counter()
            sparse_vector = self.sparse.embed_query(query)
            sparse_embedding_seconds = time.perf_counter() - started
            started = time.perf_counter()
            sparse_points = self.store.query_sparse(
                sparse_vector,
                limit=candidate_limit if mode == "hybrid" else top_k,
                query_filter=query_filter,
            )
            sparse_query_seconds = time.perf_counter() - started
        fusion_started = time.perf_counter()
        if mode == "dense":
            results = [
                self._result(
                    point,
                    mode=mode,
                    final_rank=rank,
                    final_score=float(point.score),
                    dense_rank=rank,
                    dense_score=float(point.score),
                )
                for rank, point in enumerate(dense_points[:top_k], start=1)
            ]
        elif mode == "sparse":
            results = [
                self._result(
                    point,
                    mode=mode,
                    final_rank=rank,
                    final_score=float(point.score),
                    sparse_rank=rank,
                    sparse_score=float(point.score),
                )
                for rank, point in enumerate(sparse_points[:top_k], start=1)
            ]
        else:
            results = self._hybrid(dense_points, sparse_points, top_k)
        fusion_seconds = time.perf_counter() - fusion_started
        return results, SearchTimings(
            total_seconds=time.perf_counter() - total_started,
            dense_embedding_seconds=dense_embedding_seconds,
            sparse_embedding_seconds=sparse_embedding_seconds,
            dense_query_seconds=dense_query_seconds,
            sparse_query_seconds=sparse_query_seconds,
            fusion_seconds=fusion_seconds,
        )

    def _hybrid(self, dense_points, sparse_points, top_k: int) -> list[SearchResult]:  # type: ignore[no-untyped-def]
        combined: dict[str, dict[str, Any]] = {}
        for signal, points in (("dense", dense_points), ("sparse", sparse_points)):
            for rank, point in enumerate(points, start=1):
                point_id = str(point.id)
                entry = combined.setdefault(
                    point_id,
                    {
                        "point": point,
                        "rrf": 0.0,
                        "dense_rank": None,
                        "dense_score": None,
                        "sparse_rank": None,
                        "sparse_score": None,
                    },
                )
                entry["rrf"] += 1.0 / (self.settings.rrf_k + rank)
                entry[f"{signal}_rank"] = rank
                entry[f"{signal}_score"] = float(point.score)
        ordered = sorted(
            combined.items(),
            key=lambda item: (-item[1]["rrf"], item[0]),
        )[:top_k]
        return [
            self._result(
                entry["point"],
                mode="hybrid",
                final_rank=final_rank,
                final_score=float(entry["rrf"]),
                dense_rank=entry["dense_rank"],
                dense_score=entry["dense_score"],
                sparse_rank=entry["sparse_rank"],
                sparse_score=entry["sparse_score"],
                rrf_score=float(entry["rrf"]),
                rrf_rank=final_rank,
            )
            for final_rank, (_, entry) in enumerate(ordered, start=1)
        ]

    @staticmethod
    def build_filter(filters: dict[str, Any]) -> models.Filter | None:
        conditions = [
            models.FieldCondition(key=key, match=models.MatchValue(value=value))
            for key, value in sorted(filters.items())
            if value is not None
        ]
        return models.Filter(must=conditions) if conditions else None

    @staticmethod
    def _result(
        point,  # type: ignore[no-untyped-def]
        *,
        mode: SearchMode,
        final_rank: int,
        final_score: float,
        dense_rank: int | None = None,
        dense_score: float | None = None,
        sparse_rank: int | None = None,
        sparse_score: float | None = None,
        rrf_score: float | None = None,
        rrf_rank: int | None = None,
    ) -> SearchResult:
        payload = dict(point.payload or {})
        return SearchResult(
            final_rank=final_rank,
            mode=mode,
            final_score=final_score,
            dense_rank=dense_rank,
            dense_score=dense_score,
            sparse_rank=sparse_rank,
            sparse_score=sparse_score,
            rrf_score=rrf_score,
            rrf_rank=rrf_rank,
            chunk_id=str(payload["chunk_id"]),
            document_id=str(payload["document_id"]),
            version_id=str(payload["version_id"]),
            relative_path=str(payload["relative_path"]),
            page_start=payload.get("page_start"),
            page_end=payload.get("page_end"),
            heading_path=list(payload.get("heading_path") or []),
            section_title=payload.get("section_title"),
            text=str(payload["text"]),
            chunk_type=str(payload["chunk_type"]),
            source_type=str(payload["source_type"]),
            related_image_ids=list(payload.get("related_image_ids") or []),
            provenance=dict(payload.get("provenance") or {}),
            payload=payload,
        )
