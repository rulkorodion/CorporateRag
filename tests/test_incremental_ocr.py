from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pymupdf as fitz

from corporate_rag_ingestion.incremental.ocr import IncrementalOCRProcessor
from corporate_rag_ingestion.ocr.base import OCREngine
from corporate_rag_ingestion.ocr.cache import OCRCache
from corporate_rag_ingestion.ocr.models import (
    OCRPageGeometry,
    OCRPageIdentity,
    OCRPageResult,
    OCRProvenance,
    OCRTextPayload,
)
from corporate_rag_ingestion.ocr.service import OCRService
from corporate_rag_ingestion.ocr.settings import MonkeyOCRSettings
from corporate_rag_ingestion.registry import RegistrationResult
from corporate_rag_ingestion.registry_models import Document, DocumentVersion
from corporate_rag_ingestion.scanner import ScannedFile, sha256_file
from corporate_rag_ingestion.schemas import NormalizedDocument, NormalizedPage
from corporate_rag_ingestion.storage import NormalizedStorage


class MustNotRunEngine(OCREngine):
    def prepare(self):  # type: ignore[no-untyped-def]
        raise AssertionError("OCR model must not load for a reusable page")

    def recognize_page(self, image, *, page_number, preprocessing_profile):  # type: ignore[no-untyped-def]
        raise AssertionError("OCR inference must not run for a reusable page")


def make_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = fitz.open()
    page = pdf.new_page(width=120, height=80)
    page.draw_rect(fitz.Rect(10, 10, 100, 70), fill=(0.2, 0.2, 0.2))
    pdf.save(path)
    pdf.close()


def test_same_rendered_ocr_page_is_reused_across_versions(tmp_path: Path) -> None:
    kb = tmp_path / "knowledge-base"
    source = kb / "scan.pdf"
    make_pdf(source)
    current_sha = sha256_file(source)
    storage = NormalizedStorage(tmp_path / "storage")
    cache = OCRCache(tmp_path / "storage" / "ocr")
    settings = MonkeyOCRSettings()
    service = OCRService(
        cache=cache,
        settings=settings,
        project_root=tmp_path,
        engine=MustNotRunEngine(),
    )
    processor = IncrementalOCRProcessor(
        storage=storage,
        knowledge_base_root=kb,
        cache=cache,
        settings=settings,
        project_root=tmp_path,
        service=service,
    )
    old_sha = "a" * 64
    old_identity = OCRPageIdentity(
        document_id="doc",
        version_id="old-version",
        relative_path="scan.pdf",
        page_number=1,
        source_sha256=old_sha,
    )
    cache.save(
        OCRPageResult(
            **old_identity.model_dump(),
            status="COMPLETED",
            page=OCRPageGeometry(width=500, height=333, dpi=300),
            text=OCRTextPayload(
                text_for_rag="распознанный текст",
                normalized_block_text="распознанный текст",
            ),
            provenance=OCRProvenance(
                ocr_source="monkeyocr",
                engine_name="test",
                ocr_model_id=settings.model_id,
                ocr_model_revision=settings.revision,
                ocr_settings_hash=settings.settings_hash(),
                processed_at=datetime.now(timezone.utc),
            ),
            fingerprint=service.fingerprint(old_identity),
        )
    )
    previous = NormalizedDocument(
        document_id="doc",
        version_id="old-version",
        metadata={
            "relative_path": "scan.pdf",
            "sha256": old_sha,
        },
        pages=[NormalizedPage(page_number=1, ocr_status="completed")],
    )
    created, already_present = processor.bootstrap_completed_page_hashes(previous, source)
    assert (created, already_present) == (1, 0)
    created, already_present = processor.bootstrap_completed_page_hashes(previous, source)
    assert (created, already_present) == (0, 1)
    storage.save(
        NormalizedDocument(
            document_id="doc",
            version_id="new-version",
            metadata={
                "relative_path": "scan.pdf",
                "extension": ".pdf",
                "sha256": current_sha,
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
    )
    scanned = ScannedFile(
        absolute_path=source,
        relative_path="scan.pdf",
        file_name="scan.pdf",
        extension=".pdf",
        folder_path=(),
        size_bytes=source.stat().st_size,
        modified_at=datetime.now(timezone.utc),
        sha256=current_sha,
        supported=True,
    )
    registration = RegistrationResult(
        document=Document(
            id="doc",
            relative_path="scan.pdf",
            file_name="scan.pdf",
            extension=".pdf",
            folder_path=[],
            size_bytes=source.stat().st_size,
            modified_at=datetime.now(timezone.utc),
            current_sha256=old_sha,
            role="document",
            indexable=True,
            processing_status="staged",
            metadata_json={},
        ),
        version=DocumentVersion(
            id="new-version",
            document_id="doc",
            version_number=2,
            sha256=current_sha,
            size_bytes=source.stat().st_size,
            modified_at=datetime.now(timezone.utc),
            processing_status="registered",
            metadata_json={},
        ),
        state="changed",
        scanned=scanned,
    )

    summary = processor.process(
        registration,
        previous_document=previous,
        allow_monkeyocr=True,
    )

    assert summary.reused == 1
    assert summary.processed == 0
    updated = NormalizedDocument.model_validate_json(
        (storage.normalized_root / "doc" / "new-version.json").read_text(encoding="utf-8")
    )
    assert updated.pages[0].ocr_status == "completed"
    assert updated.pages[0].needs_ocr is False
    cloned = cache.load(
        OCRPageIdentity(
            document_id="doc",
            version_id="new-version",
            relative_path="scan.pdf",
            page_number=1,
            source_sha256=current_sha,
        )
    )
    assert cloned is not None and cloned.provenance is not None
    assert cloned.provenance.processing_metadata["incremental_reused"] is True
    assert cloned.provenance.processing_metadata["reused_from_version_id"] == "old-version"
