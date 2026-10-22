from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TableRepresentation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    html: str | None = None
    original_html: str | None = None
    row_start: int | str | None = None
    row_end: int | str | None = None
    render_headers: list[str] = Field(default_factory=list)
    render_header_source: str | None = None


class RetrievalSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reranker_rank: int | None = None
    reranker_score: float | None = None
    rrf_rank: int | None = None
    rrf_score: float | None = None
    dense_rank: int | None = None
    dense_score: float | None = None
    sparse_rank: int | None = None
    sparse_score: float | None = None


class ContextSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_number: int = Field(ge=1)
    citation: str = Field(pattern=r"^\[\d+\]$")
    chunk_id: str
    document_id: str
    version_id: str
    relative_path: str
    page_start: int | None = None
    page_end: int | None = None
    heading_path: list[str] = Field(default_factory=list)
    section_title: str | None = None
    parent_section_id: str | None = None
    text: str
    chunk_type: Literal["text", "table"]
    table: TableRepresentation | None = None
    retrieval: RetrievalSignals
    source_type: Literal["native", "ocr", "mixed"]
    provenance: dict[str, Any] = Field(default_factory=dict)
    source_element_ids: list[str] = Field(default_factory=list)
    related_image_ids: list[str] = Field(default_factory=list)
    related_table_ids: list[str] = Field(default_factory=list)
    added_as_neighbor: bool = False
    parent_chunk_id: str | None = None


class RelatedImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    document_id: str
    version_id: str
    local_path: str
    source_page: int | None = None
    visual_role: str
    visual_classification_status: str
    path_scope: Literal["storage", "knowledge_base"] = "storage"


class ContextBuildStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_results: int = 0
    primary_sources: int = 0
    neighbors_added: int = 0
    lower_rank_results_replaced_by_neighbors: int = 0
    exact_duplicates_removed: int = 0
    near_duplicates_removed: int = 0
    document_limit_removed: int = 0
    budget_removed: int = 0
    missing_assets_removed: int = 0


class ContextPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    query: str
    context_id: str
    builder_version: str
    settings_hash: str
    token_counter: str
    token_count: int = Field(ge=0)
    rendered_context: str
    sources: list[ContextSource] = Field(default_factory=list)
    related_images: list[RelatedImage] = Field(default_factory=list)
    stats: ContextBuildStats = Field(default_factory=ContextBuildStats)
