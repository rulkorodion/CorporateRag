from __future__ import annotations

from corporate_rag_ingestion.ocr.imported import ImportedOCRParser
from corporate_rag_ingestion.ocr.models import OCRPageIdentity
from corporate_rag_ingestion.ocr.settings import MonkeyOCRSettings


def imported_record(settings: MonkeyOCRSettings) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "created_at": "2026-08-10T10:00:00Z",
        "status": "COMPLETED",
        "document_id": "doc",
        "version_id": "version",
        "relative_path": "folder/scan.pdf",
        "page_number": 1,
        "source_sha256": "a" * 64,
        "fingerprint": {
            "document_id": "doc",
            "version_id": "version",
            "page_number": 1,
            "source_sha256": "a" * 64,
            "ocr_settings_sha256": settings.settings_hash(),
        },
        "page": {"width": 100, "height": 200, "dpi": 300},
        "text": {
            "text_for_rag": "Recognized text\n\n<table></table>\n\n$$x+y$$",
            "normalized_block_text": "Recognized text",
            "main_text": "Recognized text",
            "native_markdown": "Recognized text",
        },
        "blocks": [
            {
                "text": "Recognized text",
                "confidence": 1.0,
                "bounding_box": {
                    "x_min": 5,
                    "y_min": 5,
                    "x_max": 90,
                    "y_max": 30,
                    "page_width": 100,
                    "page_height": 200,
                },
                "page_number": 1,
                "block_type": "Text",
                "reading_order": 0,
            }
        ],
        "layout_elements": [
            {
                "block_type": "Text",
                "page_number": 1,
                "reading_order": 0,
                "bbox_source_width": 100,
                "bbox_source_height": 200,
                "original_page_width": 100,
                "original_page_height": 200,
                "normalized_bbox": [50, 25, 900, 150],
                "original_pixel_bbox": [5, 5, 90, 30],
                "text": "Recognized text",
                "raw_response": "Recognized text",
                "native_content": "Recognized text",
                "model_confidence": 1.0,
            },
            {
                "block_type": "Table",
                "page_number": 1,
                "reading_order": 1,
                "bbox_source_width": 100,
                "bbox_source_height": 200,
                "original_page_width": 100,
                "original_page_height": 200,
                "normalized_bbox": [50, 200, 900, 500],
                "original_pixel_bbox": [5, 40, 90, 100],
                "raw_response": "<fcel>A<fcel>B",
                "native_content": "<table><tr><td>A</td><td>B</td></tr></table>",
            },
            {
                "block_type": "Formula",
                "page_number": 1,
                "reading_order": 2,
                "bbox_source_width": 100,
                "bbox_source_height": 200,
                "original_page_width": 100,
                "original_page_height": 200,
                "normalized_bbox": [50, 550, 900, 700],
                "original_pixel_bbox": [5, 110, 90, 140],
                "raw_response": "$x+y$",
                "native_content": "$$x+y$$",
            },
            {
                "block_type": "Picture",
                "page_number": 1,
                "reading_order": 3,
                "bbox_source_width": 100,
                "bbox_source_height": 200,
                "original_page_width": 100,
                "original_page_height": 200,
                "normalized_bbox": [100, 750, 800, 950],
                "original_pixel_bbox": [10, 150, 80, 190],
                "text": "THIS MUST NOT ENTER TEXT",
            },
        ],
        "tables": [
            {
                "page_number": 1,
                "reading_order": 1,
                "bbox": [5, 40, 90, 100],
                "otsl": "<fcel>A<fcel>B",
                "html": "<table><tr><td>A</td><td>B</td></tr></table>",
                "raw_response": "<fcel>A<fcel>B",
                "fallback_plain_text": "A B",
                "fallback_method": "plain_text_horizontal_tiles",
            }
        ],
        "warnings": [],
        "ocr": {
            "engine_name": "MonkeyOCRv2-B-Parsing",
            "model_id": settings.model_id,
            "revision": settings.revision,
            "settings_sha256": settings.settings_hash(),
            "runtime": {},
            "processing_metadata": {},
        },
    }


def identity() -> OCRPageIdentity:
    return OCRPageIdentity(
        document_id="doc",
        version_id="version",
        relative_path="folder/scan.pdf",
        page_number=1,
        source_sha256="a" * 64,
    )


def test_imported_ocr_parses_neutral_content_and_discards_scores() -> None:
    settings = MonkeyOCRSettings()
    result = ImportedOCRParser(settings).parse(imported_record(settings), identity())

    assert result.provenance is not None
    assert result.provenance.ocr_source == "imported"
    assert result.blocks[0].confidence is None
    assert result.layout_elements[0].model_confidence is None
    assert result.tables[0].otsl.startswith("<fcel>")
    assert result.tables[0].html.startswith("<table>")
    assert result.tables[0].fallback_plain_text == "A B"
    assert result.formulas[0].latex == "x+y"
    assert "THIS MUST NOT ENTER TEXT" not in result.text.text_for_rag


def test_imported_ocr_rejects_version_and_sha_mismatch() -> None:
    settings = MonkeyOCRSettings()
    parser = ImportedOCRParser(settings)
    record = imported_record(settings)
    record["version_id"] = "stale-version"
    try:
        parser.parse(record, identity())
    except ValueError as exc:
        assert "version_id mismatch" in str(exc)
    else:
        raise AssertionError("stale OCR version was accepted")

    record = imported_record(settings)
    record["source_sha256"] = "b" * 64
    try:
        parser.parse(record, identity())
    except ValueError as exc:
        assert "source_sha256 mismatch" in str(exc)
    else:
        raise AssertionError("stale OCR source hash was accepted")


def test_failed_server_envelope_remains_retryable_without_ocr_metadata() -> None:
    settings = MonkeyOCRSettings()
    record = imported_record(settings)
    record["status"] = "FAILED"
    record.pop("ocr")
    record.pop("page")
    record.pop("blocks")
    record.pop("layout_elements")
    record.pop("tables")
    record["error"] = {"type": "RuntimeError", "message": "worker failure"}

    result = ImportedOCRParser(settings).parse(record, identity())

    assert result.status == "FAILED"
    assert result.provenance is None
    assert result.reusable is False
