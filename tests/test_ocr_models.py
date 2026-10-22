from __future__ import annotations

from datetime import datetime, timezone

import pytest

from corporate_rag_ingestion.ocr.models import (
    OCRBlock,
    OCRBoundingBox,
    OCRFingerprint,
    OCRPageGeometry,
    OCRPageResult,
    OCRProvenance,
    OCRTextPayload,
)
from corporate_rag_ingestion.ocr.monkey_engine import (
    layout_bbox_to_original,
    otsl_to_html,
)
from corporate_rag_ingestion.ocr.reading_order import normalized_block_text
from corporate_rag_ingestion.ocr.settings import MonkeyOCRSettings


def test_settings_hash_matches_verified_server_contract() -> None:
    assert MonkeyOCRSettings().settings_hash() == (
        "fd6e8be8eb076696bdb1adc2a4803cb7d7ff39dc75a51c230b560ec3f78970bf"
    )


def test_layout_bbox_maps_to_original_render() -> None:
    assert layout_bbox_to_original(
        [100, 200, 900, 800],
        bbox_source_width=500,
        bbox_source_height=700,
        original_page_width=1000,
        original_page_height=1400,
    ) == (100, 280, 900, 1120)


def test_reading_order_prefers_explicit_model_order() -> None:
    box = OCRBoundingBox(
        x_min=0, y_min=0, x_max=10, y_max=10, page_width=100, page_height=100
    )
    blocks = [
        OCRBlock(text="second", bounding_box=box, page_number=1, reading_order=1),
        OCRBlock(text="first", bounding_box=box, page_number=1, reading_order=0),
    ]
    assert normalized_block_text(blocks, 100) == "first\nsecond"


def test_ocr_result_serializes_with_null_confidence() -> None:
    settings = MonkeyOCRSettings()
    fingerprint = OCRFingerprint(
        document_id="doc",
        version_id="version",
        relative_path="scan.pdf",
        page_number=1,
        source_sha256="a" * 64,
        ocr_model_id=settings.model_id,
        ocr_model_revision=settings.revision,
        ocr_settings_hash=settings.settings_hash(),
    )
    result = OCRPageResult(
        **fingerprint.model_dump(
            exclude={"ocr_model_id", "ocr_model_revision", "ocr_settings_hash"}
        ),
        status="COMPLETED",
        page=OCRPageGeometry(width=100, height=100, dpi=300),
        text=OCRTextPayload(text_for_rag="text"),
        blocks=[
            OCRBlock(
                text="text",
                confidence=None,
                model_confidence=None,
                bounding_box=OCRBoundingBox(
                    x_min=1,
                    y_min=2,
                    x_max=50,
                    y_max=20,
                    page_width=100,
                    page_height=100,
                ),
                page_number=1,
            )
        ],
        provenance=OCRProvenance(
            ocr_source="imported",
            engine_name="MonkeyOCR",
            ocr_model_id=settings.model_id,
            ocr_model_revision=settings.revision,
            ocr_settings_hash=settings.settings_hash(),
            processed_at=datetime.now(timezone.utc),
        ),
        fingerprint=fingerprint,
    )
    restored = OCRPageResult.model_validate_json(result.model_dump_json())
    assert restored.blocks[0].confidence is None
    assert restored.blocks[0].model_confidence is None
    with pytest.raises(ValueError):
        OCRBlock(
            text="bad",
            confidence=1.0,  # type: ignore[arg-type]
            bounding_box=restored.blocks[0].bounding_box,
            page_number=1,
        )


def test_table_otsl_is_preserved_as_html() -> None:
    otsl = "<fcel>A<fcel>B<nl><fcel>1<fcel>2"
    assert otsl_to_html(otsl) == (
        "<table><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>"
    )
