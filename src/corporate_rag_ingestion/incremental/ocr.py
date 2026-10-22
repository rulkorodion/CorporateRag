from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..ocr.cache import OCRCache
from ..ocr.importer import OCRImporter
from ..ocr.models import OCRPageIdentity, OCRPageResult
from ..ocr.service import OCRService, render_pdf_page
from ..ocr.settings import MonkeyOCRSettings
from ..registry import RegistrationResult
from ..schemas import NormalizedDocument
from ..storage import NormalizedStorage


@dataclass(slots=True)
class IncrementalOCRSummary:
    processed: int = 0
    reused: int = 0


class IncrementalOCRProcessor:
    """OCR only unresolved pages and reuse page results across document versions."""

    def __init__(
        self,
        *,
        storage: NormalizedStorage,
        knowledge_base_root: Path,
        cache: OCRCache,
        settings: MonkeyOCRSettings,
        project_root: Path,
        service: OCRService | None = None,
    ) -> None:
        self.storage = storage
        self.knowledge_base_root = knowledge_base_root.resolve()
        self.cache = cache
        self.settings = settings
        self.service = service or OCRService(
            cache=cache,
            settings=settings,
            project_root=project_root,
        )
        self.importer = OCRImporter(
            storage=storage,
            knowledge_base_root=self.knowledge_base_root,
            cache=cache,
            settings=settings,
        )

    def process(
        self,
        registration: RegistrationResult,
        *,
        previous_document: NormalizedDocument | None,
        allow_monkeyocr: bool,
    ) -> IncrementalOCRSummary:
        if registration.scanned.extension != ".pdf":
            return IncrementalOCRSummary()
        normalized_path = self.storage.normalized_root / registration.document.id / (
            f"{registration.version.id}.json"
        )
        document = NormalizedDocument.model_validate_json(
            normalized_path.read_text(encoding="utf-8")
        )
        candidates = [
            page
            for page in document.pages
            if page.page_visual_type == "scan_candidate" and page.needs_ocr
        ]
        if not candidates:
            return IncrementalOCRSummary()

        source_path = registration.scanned.absolute_path.resolve()
        page_hashes = {
            page.page_number: self._page_hash(source_path, page.page_number)
            for page in candidates
        }
        document.metadata["ocr_page_content_hashes"] = {
            str(number): digest for number, digest in sorted(page_hashes.items())
        }
        self.storage.save(document)

        reusable_by_hash = self._old_results_by_hash(previous_document)
        summary = IncrementalOCRSummary()
        for page in candidates:
            identity = OCRPageIdentity(
                document_id=document.document_id,
                version_id=document.version_id,
                relative_path=registration.scanned.relative_path,
                page_number=page.page_number,
                source_sha256=registration.scanned.sha256,
            )
            fingerprint = self.service.fingerprint(identity)
            result = self.cache.load(identity)
            reused = bool(result and result.reusable and result.fingerprint == fingerprint)
            if not reused:
                old_result = reusable_by_hash.get(page_hashes[page.page_number])
                if old_result is not None:
                    result = self._clone_result(old_result, identity)
                    self.cache.save(result)
                    reused = True
            if not reused:
                if not allow_monkeyocr:
                    raise RuntimeError(
                        "new OCR page requires MonkeyOCR, but production OCR execution is disabled"
                    )
                result, service_reused = self.service.process_page(
                    source_path,
                    identity,
                    allow_monkeyocr=True,
                )
                reused = service_reused
            assert result is not None
            if result.status != "COMPLETED":
                raise RuntimeError(
                    f"OCR did not complete: {registration.scanned.relative_path}#page={page.page_number} "
                    f"status={result.status}"
                )
            self.importer.integrate_completed(result, source_path)
            if reused:
                summary.reused += 1
            else:
                summary.processed += 1
        return summary

    def bootstrap_completed_page_hashes(
        self,
        document: NormalizedDocument,
        source_path: Path,
    ) -> tuple[int, int]:
        """One-time migration enabling cross-version OCR page reuse.

        This renders pages but never loads or calls the OCR model.
        """

        existing_raw = document.metadata.get("ocr_page_content_hashes") or {}
        existing = dict(existing_raw) if isinstance(existing_raw, dict) else {}
        created = 0
        reused = 0
        for page in document.pages:
            if page.ocr_status != "completed":
                continue
            key = str(page.page_number)
            if isinstance(existing.get(key), str) and len(str(existing[key])) == 64:
                reused += 1
                continue
            existing[key] = self._page_hash(source_path, page.page_number)
            created += 1
        if created:
            document.metadata["ocr_page_content_hashes"] = {
                key: existing[key] for key in sorted(existing, key=int)
            }
            self.storage.save(document)
        return created, reused

    def _old_results_by_hash(
        self, previous_document: NormalizedDocument | None
    ) -> dict[str, OCRPageResult]:
        if previous_document is None:
            return {}
        hashes = previous_document.metadata.get("ocr_page_content_hashes") or {}
        if not isinstance(hashes, dict):
            return {}
        relative_path = str(previous_document.metadata.get("relative_path") or "")
        source_sha = str(previous_document.metadata.get("sha256") or "")
        if not relative_path or len(source_sha) != 64:
            return {}
        output: dict[str, OCRPageResult] = {}
        for raw_page, raw_hash in hashes.items():
            try:
                identity = OCRPageIdentity(
                    document_id=previous_document.document_id,
                    version_id=previous_document.version_id,
                    relative_path=relative_path,
                    page_number=int(raw_page),
                    source_sha256=source_sha,
                )
            except (TypeError, ValueError):
                continue
            result = self.cache.load(identity)
            if result is not None and result.reusable:
                output[str(raw_hash)] = result
        return output

    def _clone_result(
        self,
        old: OCRPageResult,
        identity: OCRPageIdentity,
    ) -> OCRPageResult:
        fingerprint = self.service.fingerprint(identity)
        provenance = old.provenance
        if provenance is not None:
            provenance = provenance.model_copy(
                update={
                    "processing_metadata": {
                        **provenance.processing_metadata,
                        "incremental_reused": True,
                        "reused_from_version_id": old.version_id,
                        "reused_from_page_number": old.page_number,
                    }
                }
            )
        return old.model_copy(
            update={
                **identity.model_dump(),
                "fingerprint": fingerprint,
                "provenance": provenance,
                "processing_time_seconds": 0.0,
            }
        )

    def _page_hash(self, source_path: Path, page_number: int) -> str:
        image = render_pdf_page(source_path, page_number, self.settings.pdf_render_dpi)
        digest = hashlib.sha256()
        digest.update(f"{image.mode}:{image.width}x{image.height}:".encode("ascii"))
        digest.update(image.tobytes())
        return digest.hexdigest()

    def release(self) -> None:
        self.service.release()
