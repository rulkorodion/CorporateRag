from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..chunking.chunker import StructuralChunker
from ..chunking.models import ChunkedDocument
from ..chunking.settings import ChunkingSettings
from ..chunking.storage import ChunkStorage
from ..chunking.validation import validate_chunked_document
from ..pipeline import IngestionPipeline, IngestionSummary
from ..registry import DocumentRegistry, RegistrationResult
from ..registry_models import DocumentVersion
from ..schemas import NormalizedDocument
from ..storage import NormalizedStorage
from .ocr import IncrementalOCRProcessor


@dataclass(slots=True)
class PreparedDocument:
    chunked: ChunkedDocument
    normalized: bool
    ocr_pages_processed: int = 0
    ocr_pages_reused: int = 0


class IncrementalDocumentPreparer:
    def __init__(
        self,
        *,
        ingestion: IngestionPipeline,
        normalized_storage: NormalizedStorage,
        chunk_storage: ChunkStorage,
        chunker: StructuralChunker,
        chunking_settings: ChunkingSettings,
        ocr: IncrementalOCRProcessor,
    ) -> None:
        self.ingestion = ingestion
        self.normalized_storage = normalized_storage
        self.chunk_storage = chunk_storage
        self.chunker = chunker
        self.chunking_settings = chunking_settings
        self.ocr = ocr

    def prepare(
        self,
        session: Session,
        registry: DocumentRegistry,
        registration: RegistrationResult,
        *,
        previous_version: DocumentVersion | None,
        allow_monkeyocr: bool,
    ) -> PreparedDocument:
        ingestion_summary = IngestionSummary()
        normalized = self.ingestion.normalize_one(
            session,
            registry,
            registration,
            ingestion_summary,
            update_document_status=False,
        )
        if not normalized:
            message = registration.version.error_message or (
                ingestion_summary.error_details[0]
                if ingestion_summary.error_details
                else "normalization did not complete"
            )
            raise RuntimeError(message)

        previous_document = self._load_previous(previous_version)
        ocr_summary = self.ocr.process(
            registration,
            previous_document=previous_document,
            allow_monkeyocr=allow_monkeyocr,
        )
        normalized_path = self.normalized_storage.normalized_root / registration.document.id / (
            f"{registration.version.id}.json"
        )
        document = NormalizedDocument.model_validate_json(
            normalized_path.read_text(encoding="utf-8")
        )
        unresolved = [
            page.page_number
            for page in document.pages
            if page.page_visual_type == "scan_candidate" and page.needs_ocr
        ]
        if unresolved:
            raise RuntimeError(f"unresolved OCR pages remain: {unresolved}")

        chunked = self.chunker.chunk(document)
        validate_chunked_document(document, chunked, self.chunking_settings)
        self.chunk_storage.save(chunked)
        registration.version.processing_status = "prepared"
        registration.version.metadata_json = {
            **registration.version.metadata_json,
            "incremental_state": "prepared",
            "chunker_version": chunked.chunker_version,
            "chunk_settings_hash": chunked.settings_hash,
        }
        session.flush()
        return PreparedDocument(
            chunked=chunked,
            normalized=True,
            ocr_pages_processed=ocr_summary.processed,
            ocr_pages_reused=ocr_summary.reused,
        )

    def _load_previous(
        self, version: DocumentVersion | None
    ) -> NormalizedDocument | None:
        if version is None or not version.normalized_path:
            return None
        try:
            return self.normalized_storage.load(version.normalized_path)
        except (FileNotFoundError, ValueError):
            return None

    def release_ocr(self) -> None:
        self.ocr.release()
