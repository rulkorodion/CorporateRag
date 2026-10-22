from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import (
    OCRBlock,
    OCRBoundingBox,
    OCRFingerprint,
    OCRFormula,
    OCRLayoutElement,
    OCRPageGeometry,
    OCRPageIdentity,
    OCRPageResult,
    OCRProvenance,
    OCRTable,
    OCRTextPayload,
)
from .settings import MonkeyOCRSettings


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _required_equal(record: dict[str, Any], identity: OCRPageIdentity, field: str) -> None:
    actual = record.get(field)
    expected = getattr(identity, field)
    if field == "relative_path":
        actual = str(actual or "").replace("\\", "/")
    elif field == "source_sha256":
        actual = str(actual or "").casefold()
    elif field == "page_number":
        actual = int(actual or 0)
    else:
        actual = str(actual or "")
    if actual != expected:
        raise ValueError(f"imported OCR {field} mismatch: expected={expected!r} actual={actual!r}")


def _bbox(value: Any, page: OCRPageGeometry) -> OCRBoundingBox:
    if not isinstance(value, dict):
        raise ValueError("imported OCR block has no bounding_box")
    return OCRBoundingBox(
        x_min=float(value.get("x_min", value.get("left", 0))),
        y_min=float(value.get("y_min", value.get("top", 0))),
        x_max=float(value.get("x_max", value.get("right", 0))),
        y_max=float(value.get("y_max", value.get("bottom", 0))),
        page_width=float(value.get("page_width", page.width)),
        page_height=float(value.get("page_height", page.height)),
    )


def _layout_element(value: dict[str, Any], page: OCRPageGeometry) -> OCRLayoutElement:
    pixel_bbox = value.get("original_pixel_bbox") or value.get("bbox")
    if not isinstance(pixel_bbox, list) or len(pixel_bbox) != 4:
        raise ValueError("imported OCR layout element has invalid original_pixel_bbox")
    return OCRLayoutElement(
        block_type=str(value.get("block_type") or value.get("label") or "Text"),
        page_number=int(value.get("page_number") or 0),
        reading_order=int(value.get("reading_order") or 0),
        bbox_source_width=int(value.get("bbox_source_width") or page.width),
        bbox_source_height=int(value.get("bbox_source_height") or page.height),
        original_page_width=int(value.get("original_page_width") or page.width),
        original_page_height=int(value.get("original_page_height") or page.height),
        normalized_bbox=list(value.get("normalized_bbox") or [0, 0, 0, 0]),
        original_pixel_bbox=[int(item) for item in pixel_bbox],
        text=str(value.get("text") or ""),
        raw_response=str(value.get("raw_response") or ""),
        native_content=str(value.get("native_content") or ""),
        crop_sha256=value.get("crop_sha256"),
        model_confidence=None,
        metadata=dict(value.get("metadata") or {}),
    )


class ImportedOCRParser:
    """Parse autonomous/server MonkeyOCR JSON into the stable RAG OCR contract."""

    def __init__(self, settings: MonkeyOCRSettings) -> None:
        self.settings = settings

    def parse(self, record: dict[str, Any], expected: OCRPageIdentity) -> OCRPageResult:
        for field in ("document_id", "version_id", "relative_path", "page_number", "source_sha256"):
            _required_equal(record, expected, field)

        status = str(record.get("status") or "FAILED").upper()
        if status not in {"COMPLETED", "PARTIAL", "FAILED"}:
            raise ValueError(f"unsupported imported OCR status: {status}")
        ocr = dict(record.get("ocr") or {})
        external_fingerprint = dict(record.get("fingerprint") or {})
        model_id = str(ocr.get("model_id") or "")
        revision = str(ocr.get("revision") or ocr.get("model_revision") or "")
        settings_hash = str(
            ocr.get("settings_sha256")
            or ocr.get("ocr_settings_hash")
            or external_fingerprint.get("ocr_settings_sha256")
            or ""
        ).casefold()
        # The autonomous runner's FAILED envelope is written before an OCR page
        # exists and therefore intentionally has no ``ocr`` section.  It still
        # carries the exact settings hash in its fingerprint and is safe to
        # retain as a retryable (never integrable) cache record.
        if status == "FAILED" and not model_id and not revision:
            model_id = self.settings.model_id
            revision = self.settings.revision
        if model_id != self.settings.model_id:
            raise ValueError(f"imported OCR model mismatch: {model_id!r}")
        if revision != self.settings.revision:
            raise ValueError(f"imported OCR revision mismatch: {revision!r}")
        if settings_hash != self.settings.settings_hash():
            raise ValueError("imported OCR effective settings hash mismatch")

        fingerprint = OCRFingerprint(
            **expected.model_dump(),
            ocr_model_id=model_id,
            ocr_model_revision=revision,
            ocr_settings_hash=settings_hash,
        )
        page_value = record.get("page")
        page = OCRPageGeometry.model_validate(page_value) if isinstance(page_value, dict) else None
        text = OCRTextPayload.model_validate(record.get("text") or {})
        blocks: list[OCRBlock] = []
        if page is not None:
            for raw_block in record.get("blocks") or []:
                if not isinstance(raw_block, dict):
                    continue
                blocks.append(
                    OCRBlock(
                        text=str(raw_block.get("text") or ""),
                        confidence=None,
                        model_confidence=None,
                        bounding_box=_bbox(raw_block.get("bounding_box"), page),
                        page_number=int(raw_block.get("page_number") or expected.page_number),
                        block_type=str(raw_block.get("block_type") or "Text"),
                        reading_order=raw_block.get("reading_order"),
                        source=str(raw_block.get("source") or "imported-monkeyocr"),
                        metadata=dict(raw_block.get("metadata") or {}),
                    )
                )

        layout = [
            _layout_element(item, page)
            for item in (record.get("layout_elements") or [])
            if isinstance(item, dict) and page is not None
        ]
        tables = [OCRTable.model_validate(item) for item in (record.get("tables") or [])]
        formulas = [
            OCRFormula(
                page_number=item.page_number,
                reading_order=item.reading_order,
                bbox=item.original_pixel_bbox,
                latex=item.raw_response.strip().strip("$"),
                native_content=item.native_content,
            )
            for item in layout
            if item.block_type == "Formula" and item.raw_response.strip()
        ]
        headers = [item for item in layout if item.block_type == "Page-header"]
        footers = [item for item in layout if item.block_type == "Page-footer"]
        created_at = record.get("created_at") or record.get("processed_at")
        processed_at = datetime.fromisoformat(str(created_at).replace("Z", "+00:00")) if created_at else _utc_now()
        provenance = OCRProvenance(
            ocr_source="imported",
            engine_name=str(ocr.get("engine_name") or "MonkeyOCRv2-B-Parsing"),
            ocr_model_id=model_id,
            ocr_model_revision=revision,
            ocr_settings_hash=settings_hash,
            processed_at=processed_at,
            runtime=dict(ocr.get("runtime") or {}),
            processing_metadata=dict(ocr.get("processing_metadata") or {}),
        ) if page is not None else None
        return OCRPageResult(
            **expected.model_dump(),
            status=status,
            page=page,
            text=text,
            blocks=blocks,
            layout_elements=layout,
            tables=tables,
            formulas=formulas,
            page_headers=headers,
            page_footers=footers,
            warnings=list(record.get("warnings") or []),
            provenance=provenance,
            fingerprint=fingerprint,
            processing_time_seconds=record.get("processing_time_seconds"),
            error=record.get("error"),
        )


def read_import_records(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        aggregate = path / "all_pages.jsonl"
        if aggregate.is_file():
            return _read_jsonl(aggregate)
        records: list[dict[str, Any]] = []
        for child in sorted(path.rglob("*.json")):
            value = json.loads(child.read_text(encoding="utf-8-sig"))
            if isinstance(value, dict) and "document_id" in value and "page_number" in value:
                records.append(value)
        for child in sorted(path.rglob("*.jsonl")):
            records.extend(_read_jsonl(child))
        return records
    if path.suffix.casefold() == ".jsonl":
        return _read_jsonl(path)
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    raise ValueError(f"unsupported imported OCR payload: {path}")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"OCR JSONL record {path}:{line_number} must be an object")
        records.append(value)
    return records
