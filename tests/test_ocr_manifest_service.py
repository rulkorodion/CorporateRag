from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pymupdf as fitz
from PIL import Image

from corporate_rag_ingestion.ocr.base import OCREngine
from corporate_rag_ingestion.ocr.cache import OCRCache
from corporate_rag_ingestion.ocr.manifest import OCRManifestGenerator
from corporate_rag_ingestion.ocr.integration import OCRIntegrator
from corporate_rag_ingestion.ocr.models import (
    OCRBlock,
    OCRBoundingBox,
    OCREnginePage,
    OCRFingerprint,
    OCRPageGeometry,
    OCRPageIdentity,
    OCRPageResult,
    OCRProvenance,
    OCRTextPayload,
)
from corporate_rag_ingestion.ocr.service import OCRService
from corporate_rag_ingestion.ocr.settings import MonkeyOCRSettings
from corporate_rag_ingestion.scanner import sha256_file
from corporate_rag_ingestion.schemas import NormalizedDocument, NormalizedPage
from corporate_rag_ingestion.storage import NormalizedStorage


def create_pdf(path: Path, pages: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = fitz.open()
    for _ in range(pages):
        pdf.new_page(width=100, height=100)
    pdf.save(path)
    pdf.close()


class FakeEngine(OCREngine):
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.recognize_calls = 0

    def prepare(self) -> dict[str, object]:
        self.prepare_calls += 1
        return {"fake": True}

    def recognize_page(
        self,
        image: Image.Image,
        *,
        page_number: int,
        preprocessing_profile: str,
    ) -> OCREnginePage:
        self.recognize_calls += 1
        box = OCRBoundingBox(
            x_min=1,
            y_min=1,
            x_max=20,
            y_max=20,
            page_width=image.width,
            page_height=image.height,
        )
        return OCREnginePage(
            page_number=page_number,
            page=OCRPageGeometry(width=image.width, height=image.height, dpi=300),
            text=OCRTextPayload(text_for_rag="recognized", normalized_block_text="recognized"),
            blocks=[
                OCRBlock(
                    text="recognized",
                    confidence=None,
                    bounding_box=box,
                    page_number=page_number,
                    reading_order=0,
                )
            ],
            engine_name="fake-monkeyocr",
        )


def completed_result(
    identity: OCRPageIdentity,
    settings: MonkeyOCRSettings,
    fingerprint: OCRFingerprint,
    status: str = "COMPLETED",
) -> OCRPageResult:
    complete = status == "COMPLETED"
    base_identity = OCRPageIdentity(
        document_id=identity.document_id,
        version_id=identity.version_id,
        relative_path=identity.relative_path,
        page_number=identity.page_number,
        source_sha256=identity.source_sha256,
    )
    return OCRPageResult(
        **base_identity.model_dump(),
        status=status,  # type: ignore[arg-type]
        page=OCRPageGeometry(width=100, height=100, dpi=300) if complete else None,
        text=OCRTextPayload(text_for_rag="cached") if complete else OCRTextPayload(),
        provenance=(
            OCRProvenance(
                ocr_source="imported",
                engine_name="MonkeyOCR",
                ocr_model_id=settings.model_id,
                ocr_model_revision=settings.revision,
                ocr_settings_hash=settings.settings_hash(),
                processed_at=datetime.now(timezone.utc),
            )
            if complete
            else None
        ),
        fingerprint=fingerprint,
    )


def test_manifest_selects_only_unresolved_scan_candidate(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    source = kb / "scan.pdf"
    create_pdf(source)
    source_hash = sha256_file(source)
    storage = NormalizedStorage(tmp_path / "storage")
    storage.save(
        NormalizedDocument(
            document_id="doc",
            version_id="version",
            metadata={"relative_path": "scan.pdf", "extension": ".pdf", "sha256": source_hash},
            pages=[
                NormalizedPage(page_number=1, page_visual_type="scan_candidate", needs_ocr=True),
                NormalizedPage(page_number=2, page_visual_type="text", needs_ocr=False),
                NormalizedPage(page_number=3, page_visual_type="mixed", needs_ocr=False),
                NormalizedPage(page_number=4, page_visual_type="visual_or_slide", needs_ocr=False),
                NormalizedPage(page_number=5, page_visual_type="ambiguous", needs_ocr=False),
            ],
        )
    )
    settings = MonkeyOCRSettings()
    cache = OCRCache(tmp_path / "storage/ocr")
    generator = OCRManifestGenerator(
        normalized_root=storage.normalized_root,
        knowledge_base_root=kb,
        cache=cache,
        settings=settings,
    )
    tasks, summary = generator.generate()
    assert [task.page_number for task in tasks] == [1]
    assert summary.candidate_pages == 1

    cache.save(completed_result(tasks[0], settings, tasks[0].fingerprint))
    tasks, summary = generator.generate()
    assert tasks == []
    assert summary.reusable_completed == 1


def test_completed_fingerprint_skips_but_partial_and_failed_retry(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    create_pdf(source, pages=1)
    identity = OCRPageIdentity(
        document_id="doc",
        version_id="version",
        relative_path="scan.pdf",
        page_number=1,
        source_sha256=sha256_file(source),
    )
    settings = MonkeyOCRSettings()
    cache = OCRCache(tmp_path / "ocr")
    fake = FakeEngine()
    service = OCRService(
        cache=cache,
        settings=settings,
        project_root=tmp_path,
        engine=fake,
    )
    fingerprint = service.fingerprint(identity)

    cache.save(completed_result(identity, settings, fingerprint))
    result, reused = service.process_page(source, identity, allow_monkeyocr=True)
    assert reused is True
    assert result.text.text_for_rag == "cached"
    assert fake.recognize_calls == 0

    cache.save(completed_result(identity, settings, fingerprint, status="PARTIAL"))
    result, reused = service.process_page(source, identity, allow_monkeyocr=True)
    assert reused is False
    assert result.status == "COMPLETED"
    assert fake.recognize_calls == 1

    cache.save(completed_result(identity, settings, fingerprint, status="FAILED"))
    service.process_page(source, identity, allow_monkeyocr=True)
    assert fake.recognize_calls == 2
    assert fake.prepare_calls == 1  # model/resources are prepared once for the process

    monkey_result, _ = service.process_page(source, identity, allow_monkeyocr=True)
    assert monkey_result.provenance is not None
    assert monkey_result.provenance.ocr_source == "monkeyocr"
    document = NormalizedDocument(
        document_id="doc",
        version_id="version",
        metadata={"relative_path": "scan.pdf", "extension": ".pdf", "sha256": identity.source_sha256},
        pages=[
            NormalizedPage(
                page_number=1,
                page_visual_type="scan_candidate",
                needs_ocr=True,
            )
        ],
    )
    OCRIntegrator().integrate(
        document,
        monkey_result,
        source_path=source,
        storage_root=tmp_path / "normalized-storage",
    )
    assert document.pages[0].ocr_status == "completed"
    assert "recognized" in document.text
