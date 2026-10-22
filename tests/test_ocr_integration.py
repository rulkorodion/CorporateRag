from __future__ import annotations

import json
from pathlib import Path

import pymupdf as fitz

from corporate_rag_ingestion.ocr.cache import OCRCache
from corporate_rag_ingestion.ocr.canonicalize import OCRImportCanonicalizer
from corporate_rag_ingestion.ocr.imported import ImportedOCRParser
from corporate_rag_ingestion.ocr.importer import OCRImporter
from corporate_rag_ingestion.ocr.integration import OCRIntegrator
from corporate_rag_ingestion.ocr.models import OCRPageIdentity
from corporate_rag_ingestion.ocr.settings import MonkeyOCRSettings
from corporate_rag_ingestion.scanner import sha256_file
from corporate_rag_ingestion.schemas import NormalizedDocument, NormalizedPage
from corporate_rag_ingestion.storage import NormalizedStorage

from test_ocr_imported import imported_record


def make_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = fitz.open()
    page = pdf.new_page(width=100, height=200)
    page.draw_rect(fitz.Rect(10, 150, 80, 190), color=(1, 0, 0), fill=(1, 0, 0))
    pdf.save(path)
    pdf.close()


def test_imported_ocr_enriches_existing_document_and_picture_relations(tmp_path: Path) -> None:
    source = tmp_path / "kb/folder/scan.pdf"
    make_pdf(source)
    source_hash = sha256_file(source)
    document = NormalizedDocument(
        document_id="doc",
        version_id="version",
        metadata={
            "relative_path": "folder/scan.pdf",
            "extension": ".pdf",
            "sha256": source_hash,
        },
        pages=[
            NormalizedPage(
                page_number=1,
                page_visual_type="scan_candidate",
                needs_ocr=True,
                ocr_status="pending",
            )
        ],
    )
    settings = MonkeyOCRSettings()
    record = imported_record(settings)
    record["source_sha256"] = source_hash
    fingerprint = record["fingerprint"]
    assert isinstance(fingerprint, dict)
    fingerprint["source_sha256"] = source_hash
    result = ImportedOCRParser(settings).parse(
        record,
        OCRPageIdentity(
            document_id="doc",
            version_id="version",
            relative_path="folder/scan.pdf",
            page_number=1,
            source_sha256=source_hash,
        ),
    )
    before_hash = sha256_file(source)
    integrated = OCRIntegrator().integrate(
        document,
        result,
        source_path=source,
        storage_root=tmp_path / "storage",
    )

    assert sha256_file(source) == before_hash
    page = integrated.pages[0]
    assert page.page_visual_type == "scan_candidate"
    assert page.ocr_status == "completed"
    assert page.needs_ocr is False
    assert page.metadata["ocr_trigger_classification"] == "scan_candidate"
    assert "Recognized text" in integrated.text
    assert "THIS MUST NOT ENTER TEXT" not in integrated.text
    assert any(
        element.type == "table" and element.metadata["otsl"]
        for element in integrated.elements
    )
    assert any(
        element.type == "formula" and element.metadata["latex"] == "x+y"
        for element in integrated.elements
    )
    picture = next(
        asset for asset in integrated.assets if asset.visual_role == "ocr_picture_region"
    )
    assert (tmp_path / "storage" / picture.local_path).is_file()
    assert picture.previous_text_element_id is not None
    assert picture.nearby_text_element_ids
    assert any(
        relation.type == "related_to" and relation.target_id == picture.asset_id
        for relation in integrated.relations
    )

    element_count = len(integrated.elements)
    history_count = len(page.metadata["ocr_history"])
    OCRIntegrator().integrate(
        integrated,
        result,
        source_path=source,
        storage_root=tmp_path / "storage",
    )
    assert len(integrated.elements) == element_count
    assert len(page.metadata["ocr_history"]) == history_count


def test_import_validate_only_has_no_storage_side_effects(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    source = kb / "folder/scan.pdf"
    make_pdf(source)
    source_hash = sha256_file(source)
    storage = NormalizedStorage(tmp_path / "storage")
    document = NormalizedDocument(
        document_id="doc",
        version_id="version",
        metadata={
            "relative_path": "folder/scan.pdf",
            "extension": ".pdf",
            "sha256": source_hash,
        },
        pages=[
            NormalizedPage(
                page_number=1,
                page_visual_type="scan_candidate",
                needs_ocr=True,
                ocr_status="pending",
            )
        ],
    )
    normalized_path = storage.save(document)
    settings = MonkeyOCRSettings()
    record = imported_record(settings)
    record["source_sha256"] = source_hash
    fingerprint = record["fingerprint"]
    assert isinstance(fingerprint, dict)
    fingerprint["source_sha256"] = source_hash
    import_file = tmp_path / "all_pages.jsonl"
    import_file.write_text(json.dumps(record) + "\n", encoding="utf-8")
    cache = OCRCache(tmp_path / "storage/ocr")
    summary = OCRImporter(
        storage=storage,
        knowledge_base_root=kb,
        cache=cache,
        settings=settings,
    ).import_path(import_file, validate_only=True)

    assert summary.validated == 1
    assert summary.rejected == 0
    assert summary.cached == 0
    assert cache.iter_results() == []
    unchanged = NormalizedDocument.model_validate_json(
        normalized_path.read_text(encoding="utf-8")
    )
    assert unchanged.pages[0].ocr_status == "pending"


def test_temporary_batch_ids_are_canonicalized_by_path_and_sha(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    source = kb / "folder/scan.pdf"
    make_pdf(source)
    source_hash = sha256_file(source)
    storage = NormalizedStorage(tmp_path / "storage")
    storage.save(
        NormalizedDocument(
            document_id="canonical-doc",
            version_id="canonical-version",
            metadata={
                "relative_path": "folder/scan.pdf",
                "extension": ".pdf",
                "sha256": source_hash,
            },
            pages=[
                NormalizedPage(
                    page_number=1,
                    page_visual_type="ambiguous",
                    needs_ocr=False,
                    ocr_status="not_required",
                )
            ],
        )
    )
    settings = MonkeyOCRSettings()
    record = imported_record(settings)
    record["document_id"] = "doc-001"
    record["version_id"] = source_hash[:16]
    record["source_sha256"] = source_hash
    fingerprint = record["fingerprint"]
    assert isinstance(fingerprint, dict)
    fingerprint.update(
        {
            "document_id": "doc-001",
            "version_id": source_hash[:16],
            "source_sha256": source_hash,
        }
    )
    raw_path = tmp_path / "raw.jsonl"
    ready_path = tmp_path / "ready/all_pages.jsonl"
    raw_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    canonicalizer = OCRImportCanonicalizer(
        normalized_root=storage.normalized_root,
        knowledge_base_root=kb,
        settings=settings,
    )
    blocked = canonicalizer.canonicalize(raw_path, ready_path)
    assert blocked.rejected == 1
    assert not ready_path.exists()
    summary = canonicalizer.canonicalize(
        raw_path,
        ready_path,
        allow_ambiguous=True,
    )

    assert summary.canonicalized == 1
    assert summary.rejected == 0
    rewritten = json.loads(ready_path.read_text(encoding="utf-8"))
    assert rewritten["document_id"] == "canonical-doc"
    assert rewritten["version_id"] == "canonical-version"
    assert rewritten["ocr"]["processing_metadata"]["import_identity_rebound_from"] == {
        "document_id": "doc-001",
        "version_id": source_hash[:16],
    }
    assert rewritten["ocr"]["processing_metadata"]["ocr_selection_override"] == {
        "classification": "ambiguous",
        "basis": "explicit_user_visual_review",
    }

    cache = OCRCache(tmp_path / "storage/ocr")
    blocked_import = OCRImporter(
        storage=storage,
        knowledge_base_root=kb,
        cache=cache,
        settings=settings,
    ).import_path(ready_path, validate_only=True)
    assert blocked_import.rejected == 1
    imported = OCRImporter(
        storage=storage,
        knowledge_base_root=kb,
        cache=cache,
        settings=settings,
        allow_ambiguous=True,
    ).import_path(ready_path)
    assert imported.integrated == 1
    enriched_path = storage.normalized_root / "canonical-doc" / "canonical-version.json"
    enriched = NormalizedDocument.model_validate_json(
        enriched_path.read_text(encoding="utf-8")
    )
    assert enriched.pages[0].page_visual_type == "ambiguous"
    assert enriched.pages[0].ocr_status == "completed"
    assert enriched.pages[0].metadata["ocr_history"][0]["selection_basis"] == (
        "explicit_user_visual_review"
    )
