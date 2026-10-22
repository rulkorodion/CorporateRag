from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from .adapters import AdapterContext
from .config import Settings
from .media_overrides import load_media_overrides
from .registry import DocumentRegistry, RegistrationResult
from .router import FormatRouter, UnsupportedFormatError
from .scanner import FileScanner
from .schemas import NormalizedRelation
from .storage import NormalizedStorage


LOGGER = logging.getLogger(__name__)
NORMALIZER_VERSION = "2.1-native-pdf-and-visual-relations"
VISUAL_SEARCH_VERSION = "standalone-visual-title-v1"
EXCEL_MERGED_VERSION = "merged-cells-propagated-v1"


@dataclass(slots=True)
class IngestionSummary:
    found_files: int = 0
    by_extension: Counter[str] = field(default_factory=Counter)
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    normalized: int = 0
    errors: int = 0
    unsupported: int = 0
    legacy_doc_normalized: int = 0
    legacy_doc_failed: int = 0
    legacy_rtf_normalized: int = 0
    legacy_rtf_failed: int = 0
    mp4_linked: int = 0
    mp4_missing: int = 0
    mp4_not_required: int = 0
    pdf_page_types: Counter[str] = field(default_factory=Counter)
    pdf_needs_ocr: int = 0
    extracted_images: int = 0
    scan_candidate_pages: list[str] = field(default_factory=list)
    ambiguous_pages: list[str] = field(default_factory=list)
    potentially_needs_ocr: int = 0
    warnings: list[str] = field(default_factory=list)
    error_details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "found_files": self.found_files,
            "by_extension": dict(sorted(self.by_extension.items())),
            "new": self.new,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "normalized": self.normalized,
            "errors": self.errors,
            "unsupported": self.unsupported,
            "legacy": {
                ".doc": {
                    "normalized": self.legacy_doc_normalized,
                    "failed": self.legacy_doc_failed,
                },
                ".rtf": {
                    "normalized": self.legacy_rtf_normalized,
                    "failed": self.legacy_rtf_failed,
                },
            },
            "pdf_pages": {
                **{
                    page_type: self.pdf_page_types.get(page_type, 0)
                    for page_type in (
                        "text", "mixed", "scan_candidate", "visual_or_slide", "ambiguous"
                    )
                },
                "needs_ocr": self.pdf_needs_ocr,
            },
            "assets": {"extracted_images": self.extracted_images},
            "mp4": {
                "linked": self.mp4_linked,
                "missing": self.mp4_missing,
                "not_required": self.mp4_not_required,
            },
            # Backward-compatible aliases for existing automation consumers.
            "mp4_transcripts_found": self.mp4_linked,
            "mp4_transcripts_missing": self.mp4_missing,
            "potentially_needs_ocr": self.potentially_needs_ocr,
            "scan_candidate_pages": self.scan_candidate_pages,
            "ambiguous_pages": self.ambiguous_pages,
            "warnings": self.warnings,
            "error_details": self.error_details,
        }


class IngestionPipeline:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        router: FormatRouter | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        libreoffice_executable = settings.libreoffice_executable
        if libreoffice_executable is not None:
            # Docling currently discovers LibreOffice through PATH, while newer
            # releases also advertise DOCLING_LIBREOFFICE_CMD. Configure both.
            os.environ["DOCLING_LIBREOFFICE_CMD"] = str(libreoffice_executable)
            executable_directory = str(libreoffice_executable.parent)
            path_entries = os.environ.get("PATH", "").split(os.pathsep)
            if executable_directory.casefold() not in {
                entry.casefold() for entry in path_entries if entry
            }:
                os.environ["PATH"] = os.pathsep.join(
                    [executable_directory, *[entry for entry in path_entries if entry]]
                )
        self.router = router or FormatRouter(
            docling_document_timeout_seconds=settings.docling_document_timeout_seconds,
            docling_artifacts_path=settings.docling_artifacts_root,
            legacy_cache_root=settings.legacy_cache_root,
            libreoffice_path=libreoffice_executable,
            legacy_conversion_timeout_seconds=settings.legacy_conversion_timeout_seconds,
        )
        self.storage = NormalizedStorage(settings.storage_root)

    def run(self, *, scan_only: bool = False, reprocess: bool = False) -> IngestionSummary:
        summary = IngestionSummary()
        scanned_files = FileScanner(self.settings.knowledge_base_root).scan()
        summary.found_files = len(scanned_files)
        summary.by_extension.update(item.extension or "<none>" for item in scanned_files)

        with self.session_factory() as session:
            registry = DocumentRegistry(session)
            registrations = [registry.register(item) for item in scanned_files]
            for registration in registrations:
                setattr(summary, registration.state, getattr(summary, registration.state) + 1)
                if not registration.scanned.supported:
                    summary.unsupported += 1

            overrides = load_media_overrides(self.settings.media_overrides_file)
            linked, missing, not_required, warnings = registry.pair_media_transcripts(
                registrations,
                transcript_not_required=overrides.normalized_not_required_paths,
            )
            summary.mp4_linked = linked
            summary.mp4_missing = missing
            summary.mp4_not_required = not_required
            summary.warnings.extend(warnings)
            session.commit()

            if scan_only:
                return summary

            for registration in registrations:
                if not registration.scanned.supported:
                    continue
                should_process = (
                    reprocess
                    or registration.state in {"new", "changed"}
                    or registration.version.processing_status != "normalized"
                    or registration.version.metadata_json.get("normalizer_version")
                    != NORMALIZER_VERSION
                    or (
                        registration.scanned.extension in {".jpg", ".jpeg", ".png"}
                        and registration.version.metadata_json.get("visual_search_version")
                        != VISUAL_SEARCH_VERSION
                    )
                    or (
                        registration.scanned.extension == ".xlsx"
                        and registration.version.metadata_json.get("excel_merged_version")
                        != EXCEL_MERGED_VERSION
                    )
                )
                if not should_process:
                    continue
                self.normalize_one(session, registry, registration, summary)
                session.commit()

        return summary

    def normalize_one(
        self,
        session: Session,
        registry: DocumentRegistry,
        registration: RegistrationResult,
        summary: IngestionSummary,
        *,
        update_document_status: bool = True,
    ) -> bool:
        document = registration.document
        version = registration.version
        try:
            with session.begin_nested():
                adapter = self.router.adapter_for(registration.scanned.absolute_path)
                metadata = {
                    "relative_path": registration.scanned.relative_path,
                    "file_name": registration.scanned.file_name,
                    "extension": registration.scanned.extension,
                    "folder_structure": list(registration.scanned.folder_path),
                    "size_bytes": registration.scanned.size_bytes,
                    "modified_at": registration.scanned.modified_at.isoformat(),
                    "sha256": registration.scanned.sha256,
                    "role": document.role,
                    "indexable": document.indexable,
                    **document.metadata_json,
                }
                context = AdapterContext(
                    document_id=document.id,
                    version_id=version.id,
                    source_path=registration.scanned.absolute_path,
                    knowledge_base_root=self.settings.knowledge_base_root,
                    storage_root=self.settings.storage_root,
                    assets_directory=self.storage.asset_directory(document.id),
                    metadata=metadata,
                )
                normalized = None
                if (
                    registration.state == "unchanged"
                    and version.normalized_path
                    and hasattr(adapter, "upgrade_existing")
                ):
                    try:
                        existing = self.storage.load(version.normalized_path)
                        normalized = adapter.upgrade_existing(context, existing)
                    except (FileNotFoundError, ValueError):
                        LOGGER.warning(
                            "Could not upgrade existing normalized JSON; reparsing %s",
                            registration.scanned.relative_path,
                        )
                if normalized is None:
                    normalized = adapter.normalize(context)
                normalized.metadata["normalizer_version"] = NORMALIZER_VERSION
                if registration.scanned.extension in {".jpg", ".jpeg", ".png"}:
                    normalized.metadata["visual_search_version"] = VISUAL_SEARCH_VERSION
                elif registration.scanned.extension == ".xlsx":
                    normalized.metadata["excel_merged_version"] = EXCEL_MERGED_VERSION
                existing_relation_keys = {
                    (relation.source_id, relation.target_id, relation.type)
                    for relation in normalized.relations
                }
                for relation in registry.relations_for(document.id):
                    if relation.source_document_id != document.id:
                        continue
                    relation_key = (
                        relation.source_document_id,
                        relation.target_document_id,
                        relation.relation_type,
                    )
                    if relation_key in existing_relation_keys:
                        continue
                    normalized.relations.append(
                        NormalizedRelation(
                            source_id=relation.source_document_id,
                            target_id=relation.target_document_id,
                            type=relation.relation_type,  # type: ignore[arg-type]
                            metadata=relation.metadata_json,
                        )
                    )
                    existing_relation_keys.add(relation_key)
                output_path = self.storage.save(normalized)
                version.processing_status = "normalized"
                version.normalized_path = self.storage.relative_path(output_path)
                version.error_message = None
                version.metadata_json = {
                    **version.metadata_json,
                    "normalizer_version": NORMALIZER_VERSION,
                }
                if registration.scanned.extension in {".jpg", ".jpeg", ".png"}:
                    version.metadata_json["visual_search_version"] = VISUAL_SEARCH_VERSION
                elif registration.scanned.extension == ".xlsx":
                    version.metadata_json["excel_merged_version"] = EXCEL_MERGED_VERSION
                if update_document_status:
                    document.processing_status = (
                        "media_asset" if document.role == "media_asset" else "normalized"
                    )
                session.flush()
            summary.normalized += 1
            if registration.scanned.extension == ".doc":
                summary.legacy_doc_normalized += 1
            elif registration.scanned.extension == ".rtf":
                summary.legacy_rtf_normalized += 1
            if registration.scanned.extension == ".pdf":
                summary.pdf_page_types.update(page.page_visual_type for page in normalized.pages)
                needs_ocr = sum(page.needs_ocr for page in normalized.pages)
                summary.pdf_needs_ocr += needs_ocr
                summary.potentially_needs_ocr += needs_ocr
                for page in normalized.pages:
                    reference = f"{registration.scanned.relative_path}#page={page.page_number}"
                    if page.page_visual_type == "scan_candidate":
                        summary.scan_candidate_pages.append(reference)
                    elif page.page_visual_type == "ambiguous":
                        summary.ambiguous_pages.append(reference)
            summary.extracted_images += sum(
                asset.type == "image" and asset.visual_role == "embedded_image"
                for asset in normalized.assets
            )
            LOGGER.info("Normalized %s", registration.scanned.relative_path)
            return True
        except UnsupportedFormatError:
            if update_document_status:
                document.processing_status = "unsupported"
            version.processing_status = "unsupported"
            summary.unsupported += 1
            return False
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if update_document_status:
                document.processing_status = "error"
            version.processing_status = "error"
            version.error_message = message
            session.flush()
            summary.errors += 1
            if registration.scanned.extension == ".doc":
                summary.legacy_doc_failed += 1
            elif registration.scanned.extension == ".rtf":
                summary.legacy_rtf_failed += 1
            summary.error_details.append(f"{registration.scanned.relative_path}: {message}")
            LOGGER.exception("Failed to normalize %s", registration.scanned.relative_path)
            return False
