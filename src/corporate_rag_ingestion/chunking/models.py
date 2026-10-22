from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ChunkType = Literal["text", "table"]
ChunkSourceType = Literal["native", "ocr", "mixed"]


class RetrievalChunk(BaseModel):
    """Stable retrieval boundary, independent of Docling and OCR internals."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: str = "1.0"
    chunk_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    chunk_type: ChunkType
    text: str = Field(min_length=1)
    token_count: int = Field(gt=0)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    heading_path: list[str] = Field(default_factory=list)
    section_title: str | None = None
    parent_section_id: str | None = None
    source_element_ids: list[str] = Field(min_length=1)
    previous_chunk_id: str | None = None
    next_chunk_id: str | None = None
    related_image_ids: list[str] = Field(default_factory=list)
    related_table_ids: list[str] = Field(default_factory=list)
    source_type: ChunkSourceType
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_pages_and_ids(self) -> "RetrievalChunk":
        if self.page_start is not None and self.page_end is not None:
            if self.page_start > self.page_end:
                raise ValueError("chunk page_start cannot exceed page_end")
        for field_name in ("source_element_ids", "related_image_ids", "related_table_ids"):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate values in {field_name}")
        object.__setattr__(self, "relative_path", self.relative_path.replace("\\", "/"))
        return self


class ChunkCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eligible_source_elements: int = 0
    covered_source_elements: int = 0
    missing_source_element_ids: list[str] = Field(default_factory=list)
    skipped_source_elements: dict[str, int] = Field(default_factory=dict)


class ChunkedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    document_id: str
    version_id: str
    relative_path: str
    source_sha256: str | None = None
    chunker_version: str
    settings_hash: str
    tokenizer_method: str
    chunks: list[RetrievalChunk] = Field(default_factory=list)
    coverage: ChunkCoverage = Field(default_factory=ChunkCoverage)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_chunk_sequence(self) -> "ChunkedDocument":
        ids = [chunk.chunk_id for chunk in self.chunks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate chunk IDs in ChunkedDocument")
        for index, chunk in enumerate(self.chunks):
            expected_previous = self.chunks[index - 1].chunk_id if index else None
            expected_next = self.chunks[index + 1].chunk_id if index + 1 < len(self.chunks) else None
            if chunk.previous_chunk_id != expected_previous:
                raise ValueError(f"invalid previous_chunk_id for {chunk.chunk_id}")
            if chunk.next_chunk_id != expected_next:
                raise ValueError(f"invalid next_chunk_id for {chunk.chunk_id}")
        return self
