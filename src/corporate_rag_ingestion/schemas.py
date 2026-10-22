from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ElementType = Literal[
    "heading", "paragraph", "list", "table", "formula", "image", "scanned_region"
]
RelationType = Literal["parent", "previous", "next", "related_to", "has_transcript", "transcript_of"]
PageVisualType = Literal["text", "mixed", "scan_candidate", "visual_or_slide", "ambiguous"]


class BoundingBox(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left: float
    top: float
    right: float
    bottom: float
    coordinate_origin: str | None = None

    @property
    def area(self) -> float:
        return max(0.0, self.right - self.left) * max(0.0, self.bottom - self.top)


class NormalizedPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_number: int
    width: float | None = None
    height: float | None = None
    text_character_count: int = 0
    text_block_count: int = 0
    native_text: str = ""
    native_text_char_count: int = 0
    native_text_block_count: int = 0
    image_count: int = 0
    largest_image_area_ratio: float = 0.0
    page_visual_type: PageVisualType = "ambiguous"
    needs_ocr: bool = False
    ocr_decision_reason: str = "not_evaluated"
    visual_classification_status: str = "not_required"
    ocr_status: Literal["not_required", "pending", "completed", "partial", "failed"] = (
        "not_required"
    )
    ocr_provenance: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedElement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    element_id: str
    type: ElementType
    text: str | None = None
    page_number: int | None = None
    bbox: BoundingBox | None = None
    order: int
    reading_order: int | None = None
    parent_id: str | None = None
    previous_id: str | None = None
    next_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    type: str
    source_page: int | None = None
    bbox: BoundingBox | None = None
    local_path: str
    visual_role: str = "unknown"
    visual_classification_status: str = "pending"
    reading_order: int | None = None
    previous_text_element_id: str | None = None
    next_text_element_id: str | None = None
    nearby_text_element_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    target_id: str
    type: RelationType
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedDocument(BaseModel):
    """Stable boundary model deliberately independent of Docling internals."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    document_id: str
    version_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    text: str = ""
    pages: list[NormalizedPage] = Field(default_factory=list)
    elements: list[NormalizedElement] = Field(default_factory=list)
    assets: list[NormalizedAsset] = Field(default_factory=list)
    relations: list[NormalizedRelation] = Field(default_factory=list)
