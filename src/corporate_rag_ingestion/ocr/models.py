from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


OCRStatus = Literal["COMPLETED", "PARTIAL", "FAILED"]
OCRSource = Literal["imported", "monkeyocr"]


class StrictOCRModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class OCRBoundingBox(StrictOCRModel):
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    page_width: float = Field(gt=0)
    page_height: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> "OCRBoundingBox":
        if self.x_min > self.x_max or self.y_min > self.y_max:
            raise ValueError("invalid OCR bounding-box coordinate order")
        if self.x_min < 0 or self.y_min < 0:
            raise ValueError("OCR bounding-box coordinates cannot be negative")
        if self.x_max > self.page_width or self.y_max > self.page_height:
            raise ValueError("OCR bounding box exceeds page dimensions")
        return self

    def relative(self) -> list[float]:
        return [
            self.x_min / self.page_width,
            self.y_min / self.page_height,
            self.x_max / self.page_width,
            self.y_max / self.page_height,
        ]


class OCRPageIdentity(StrictOCRModel):
    document_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    page_number: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")

    @model_validator(mode="after")
    def normalize_identity(self) -> "OCRPageIdentity":
        # ``validate_assignment`` is enabled for all OCR models.  Assigning via
        # the normal descriptor from an ``after`` validator would therefore
        # invoke this validator recursively.
        object.__setattr__(self, "relative_path", self.relative_path.replace("\\", "/"))
        object.__setattr__(self, "source_sha256", self.source_sha256.casefold())
        return self


class OCRFingerprint(OCRPageIdentity):
    ocr_model_id: str = Field(min_length=1)
    ocr_model_revision: str = Field(min_length=1)
    ocr_settings_hash: str = Field(pattern=r"^[0-9a-fA-F]{64}$")

    @model_validator(mode="after")
    def normalize_hash(self) -> "OCRFingerprint":
        object.__setattr__(self, "ocr_settings_hash", self.ocr_settings_hash.casefold())
        return self

    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class OCRManifestItem(OCRPageIdentity):
    input_file: str = Field(min_length=1)
    fingerprint: OCRFingerprint


class OCRPageGeometry(StrictOCRModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    dpi: int = Field(gt=0)


class OCRTextPayload(StrictOCRModel):
    text_for_rag: str = ""
    normalized_block_text: str = ""
    main_text: str = ""
    native_markdown: str = ""


class OCRBlock(StrictOCRModel):
    text: str
    confidence: None = None
    model_confidence: None = None
    bounding_box: OCRBoundingBox
    page_number: int = Field(ge=1)
    block_type: str = "Text"
    reading_order: int | None = Field(default=None, ge=0)
    source: str = "ocr"
    metadata: dict[str, Any] = Field(default_factory=dict)


class OCRLayoutElement(StrictOCRModel):
    block_type: str
    page_number: int = Field(ge=1)
    reading_order: int = Field(ge=0)
    bbox_source_width: int = Field(gt=0)
    bbox_source_height: int = Field(gt=0)
    original_page_width: int = Field(gt=0)
    original_page_height: int = Field(gt=0)
    normalized_bbox: list[float] = Field(min_length=4, max_length=4)
    original_pixel_bbox: list[int] = Field(min_length=4, max_length=4)
    text: str = ""
    raw_response: str = ""
    native_content: str = ""
    crop_sha256: str | None = None
    model_confidence: None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def bounding_box(self) -> OCRBoundingBox:
        x_min, y_min, x_max, y_max = self.original_pixel_bbox
        return OCRBoundingBox(
            x_min=x_min,
            y_min=y_min,
            x_max=x_max,
            y_max=y_max,
            page_width=self.original_page_width,
            page_height=self.original_page_height,
        )


class OCRTable(StrictOCRModel):
    page_number: int = Field(ge=1)
    reading_order: int = Field(ge=0)
    bbox: list[int] = Field(min_length=4, max_length=4)
    otsl: str = ""
    html: str = ""
    raw_response: str = ""
    fallback_plain_text: str = ""
    fallback_method: str | None = None


class OCRFormula(StrictOCRModel):
    page_number: int = Field(ge=1)
    reading_order: int = Field(ge=0)
    bbox: list[int] = Field(min_length=4, max_length=4)
    latex: str
    native_content: str = ""


class OCRProvenance(StrictOCRModel):
    ocr_source: OCRSource
    engine_name: str
    ocr_model_id: str
    ocr_model_revision: str
    ocr_settings_hash: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    processed_at: datetime
    runtime: dict[str, Any] = Field(default_factory=dict)
    processing_metadata: dict[str, Any] = Field(default_factory=dict)


class OCREnginePage(StrictOCRModel):
    page_number: int = Field(ge=1)
    page: OCRPageGeometry
    text: OCRTextPayload
    blocks: list[OCRBlock] = Field(default_factory=list)
    layout_elements: list[OCRLayoutElement] = Field(default_factory=list)
    tables: list[OCRTable] = Field(default_factory=list)
    formulas: list[OCRFormula] = Field(default_factory=list)
    page_headers: list[OCRLayoutElement] = Field(default_factory=list)
    page_footers: list[OCRLayoutElement] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    engine_name: str
    runtime: dict[str, Any] = Field(default_factory=dict)
    processing_metadata: dict[str, Any] = Field(default_factory=dict)


class OCRPageResult(OCRPageIdentity):
    schema_version: str = "1.0"
    status: OCRStatus
    page: OCRPageGeometry | None = None
    text: OCRTextPayload = Field(default_factory=OCRTextPayload)
    blocks: list[OCRBlock] = Field(default_factory=list)
    layout_elements: list[OCRLayoutElement] = Field(default_factory=list)
    tables: list[OCRTable] = Field(default_factory=list)
    formulas: list[OCRFormula] = Field(default_factory=list)
    page_headers: list[OCRLayoutElement] = Field(default_factory=list)
    page_footers: list[OCRLayoutElement] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    provenance: OCRProvenance | None = None
    fingerprint: OCRFingerprint
    processing_time_seconds: float | None = Field(default=None, ge=0)
    error: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_result_identity(self) -> "OCRPageResult":
        identity_fields = (
            "document_id",
            "version_id",
            "relative_path",
            "page_number",
            "source_sha256",
        )
        for field_name in identity_fields:
            if getattr(self, field_name) != getattr(self.fingerprint, field_name):
                raise ValueError(f"OCR fingerprint {field_name} mismatch")
        if self.status == "COMPLETED" and (self.page is None or self.provenance is None):
            raise ValueError("COMPLETED OCR requires page geometry and provenance")
        return self

    @property
    def reusable(self) -> bool:
        return self.status == "COMPLETED"
