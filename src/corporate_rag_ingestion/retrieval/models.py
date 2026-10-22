from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SparseEmbedding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    indices: list[int]
    values: list[float]


class EmbeddingRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    model_revision: str
    dimension: int
    device: str
    dtype: str
    settings_hash: str


class RerankerRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    model_revision: str
    device: str
    dtype: str
    batch_size: int
    max_length: int
    settings_hash: str
    peak_device_memory_bytes: int = 0
    process_rss_bytes: int = 0
    process_peak_rss_bytes: int = 0


class TokenCountComparison(BaseModel):
    structural_min: int = 0
    structural_avg: float = 0.0
    structural_median: float = 0.0
    structural_p95: int = 0
    structural_max: int = 0
    model_min: int = 0
    model_avg: float = 0.0
    model_median: float = 0.0
    model_p95: int = 0
    model_max: int = 0
    mean_ratio: float = 0.0
    truncated: int = 0


class IndexSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection_name: str
    total_chunks: int = 0
    indexable_chunks: int = 0
    excluded_chunks: int = 0
    unchanged_chunks: int = 0
    embedded_chunks: int = 0
    upserted_chunks: int = 0
    deleted_chunks: int = 0
    collection_points: int = 0
    embedding_seconds: float = 0.0
    sparse_seconds: float = 0.0
    upsert_seconds: float = 0.0
    total_seconds: float = 0.0
    storage_bytes: int = 0
    runtime: EmbeddingRuntime
    token_counts: TokenCountComparison
    errors: list[str] = Field(default_factory=list)


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_rank: int
    mode: Literal["dense", "sparse", "hybrid", "rerank"]
    final_score: float
    reranker_score: float | None = None
    rrf_rank: int | None = None
    dense_rank: int | None = None
    dense_score: float | None = None
    sparse_rank: int | None = None
    sparse_score: float | None = None
    rrf_score: float | None = None
    chunk_id: str
    document_id: str
    version_id: str
    relative_path: str
    page_start: int | None = None
    page_end: int | None = None
    heading_path: list[str] = Field(default_factory=list)
    section_title: str | None = None
    text: str
    chunk_type: str
    source_type: str
    related_image_ids: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
