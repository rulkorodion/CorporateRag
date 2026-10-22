from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .registry_models import Document, DocumentRelation, DocumentVersion
from .scanner import ScannedFile


DOCUMENT_NAMESPACE = uuid.UUID("51a37d85-60e4-4e57-a82e-23f2339f8f61")


@dataclass(slots=True)
class RegistrationResult:
    document: Document
    version: DocumentVersion
    state: str  # new, changed, unchanged
    scanned: ScannedFile


def stable_document_id(relative_path: str) -> str:
    return str(uuid.uuid5(DOCUMENT_NAMESPACE, relative_path.casefold()))


class DocumentRegistry:
    def __init__(self, session: Session) -> None:
        self.session = session

    def register(self, scanned: ScannedFile) -> RegistrationResult:
        document = self.session.scalar(
            select(Document).where(Document.relative_path == scanned.relative_path)
        )
        state = "unchanged"
        if document is None:
            state = "new"
            document = Document(
                id=stable_document_id(scanned.relative_path),
                relative_path=scanned.relative_path,
                file_name=scanned.file_name,
                extension=scanned.extension,
                folder_path=list(scanned.folder_path),
                size_bytes=scanned.size_bytes,
                modified_at=scanned.modified_at,
                current_sha256=scanned.sha256,
                indexable=scanned.supported and scanned.extension != ".mp4",
                role="media_asset" if scanned.extension == ".mp4" else "document",
                processing_status="registered" if scanned.supported else "unsupported",
                metadata_json={"folder_structure": list(scanned.folder_path)},
            )
            self.session.add(document)
            self.session.flush()
            version_number = 1
            version = self._new_version(document, scanned, version_number)
        elif document.current_sha256 != scanned.sha256:
            state = "changed"
            max_version = self.session.scalar(
                select(func.max(DocumentVersion.version_number)).where(
                    DocumentVersion.document_id == document.id
                )
            ) or 0
            version = self._new_version(document, scanned, max_version + 1)
            document.current_sha256 = scanned.sha256
            document.processing_status = "registered" if scanned.supported else "unsupported"
        else:
            version = self.session.get(DocumentVersion, document.current_version_id)
            if version is None:
                raise RuntimeError(f"Registry corruption: no current version for {document.id}")

        document.file_name = scanned.file_name
        document.extension = scanned.extension
        document.folder_path = list(scanned.folder_path)
        document.size_bytes = scanned.size_bytes
        document.modified_at = scanned.modified_at
        document.metadata_json = {**document.metadata_json, "folder_structure": list(scanned.folder_path)}
        self.session.flush()
        return RegistrationResult(document=document, version=version, state=state, scanned=scanned)

    def stage(self, scanned: ScannedFile, *, force: bool = False) -> RegistrationResult:
        """Create/reuse a candidate version without replacing the active version.

        ``register`` remains the batch-ingestion API. Incremental production updates use
        this method so a failed candidate cannot move ``current_version_id`` away from a
        working version.
        """

        document = self.session.scalar(
            select(Document).where(Document.relative_path == scanned.relative_path)
        )
        if document is None:
            state = "new"
            document = Document(
                id=stable_document_id(scanned.relative_path),
                relative_path=scanned.relative_path,
                file_name=scanned.file_name,
                extension=scanned.extension,
                folder_path=list(scanned.folder_path),
                size_bytes=scanned.size_bytes,
                modified_at=scanned.modified_at,
                current_sha256=scanned.sha256,
                current_version_id=None,
                indexable=scanned.supported and scanned.extension != ".mp4",
                role="media_asset" if scanned.extension == ".mp4" else "document",
                processing_status="staged" if scanned.supported else "unsupported",
                metadata_json={"folder_structure": list(scanned.folder_path)},
            )
            self.session.add(document)
            self.session.flush()
        else:
            active_matches = (
                document.current_version_id is not None
                and document.current_sha256 == scanned.sha256
                and document.processing_status != "deleted"
            )
            if active_matches and not force:
                version = self.session.get(DocumentVersion, document.current_version_id)
                if version is None:
                    raise RuntimeError(f"Registry corruption: no current version for {document.id}")
                return RegistrationResult(
                    document=document,
                    version=version,
                    state="unchanged",
                    scanned=scanned,
                )
            state = "changed"

        candidate = None if force else self._candidate_for_sha(document.id, scanned.sha256)
        if candidate is None:
            max_version = self.session.scalar(
                select(func.max(DocumentVersion.version_number)).where(
                    DocumentVersion.document_id == document.id
                )
            ) or 0
            candidate = self._new_staged_version(document, scanned, max_version + 1)
        else:
            candidate.processing_status = "registered" if scanned.supported else "unsupported"
            candidate.error_message = None
            candidate.metadata_json = {
                **candidate.metadata_json,
                "incremental_state": "staged",
                "folder_structure": list(scanned.folder_path),
            }

        self.session.flush()
        return RegistrationResult(
            document=document,
            version=candidate,
            state=state,
            scanned=scanned,
        )

    def promote(self, registration: RegistrationResult) -> None:
        """Atomically make a successfully indexed candidate the active version."""

        document = registration.document
        version = registration.version
        scanned = registration.scanned
        document.current_version_id = version.id
        document.current_sha256 = scanned.sha256
        document.file_name = scanned.file_name
        document.extension = scanned.extension
        document.folder_path = list(scanned.folder_path)
        document.size_bytes = scanned.size_bytes
        document.modified_at = scanned.modified_at
        document.indexable = scanned.supported and scanned.extension != ".mp4"
        document.processing_status = (
            "media_asset" if document.role == "media_asset" else version.processing_status
        )
        document.metadata_json = {
            **document.metadata_json,
            "folder_structure": list(scanned.folder_path),
            "deleted_at": None,
        }
        version.metadata_json = {
            **version.metadata_json,
            "incremental_state": "active",
            "activated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.session.flush()

    def fail_candidate(self, version: DocumentVersion, message: str) -> None:
        version.processing_status = "error"
        version.error_message = message
        version.metadata_json = {
            **version.metadata_json,
            "incremental_state": "failed",
        }
        self.session.flush()

    def mark_deleted(self, document: Document) -> None:
        document.processing_status = "deleted"
        document.metadata_json = {
            **document.metadata_json,
            "deleted_at": datetime.now(timezone.utc).isoformat(),
        }
        self.session.flush()

    def _candidate_for_sha(self, document_id: str, sha256: str) -> DocumentVersion | None:
        versions = self.session.scalars(
            select(DocumentVersion)
            .where(
                DocumentVersion.document_id == document_id,
                DocumentVersion.sha256 == sha256,
            )
            .order_by(DocumentVersion.version_number.desc())
        )
        for version in versions:
            state = str(version.metadata_json.get("incremental_state") or "")
            if state in {"staged", "prepared", "index_switching", "failed"}:
                return version
        return None

    def _new_staged_version(
        self, document: Document, scanned: ScannedFile, version_number: int
    ) -> DocumentVersion:
        version = DocumentVersion(
            id=str(uuid.uuid4()),
            document_id=document.id,
            version_number=version_number,
            sha256=scanned.sha256,
            size_bytes=scanned.size_bytes,
            modified_at=scanned.modified_at,
            processing_status="registered" if scanned.supported else "unsupported",
            metadata_json={
                "folder_structure": list(scanned.folder_path),
                "incremental_state": "staged",
            },
        )
        self.session.add(version)
        self.session.flush()
        return version

    def _new_version(
        self, document: Document, scanned: ScannedFile, version_number: int
    ) -> DocumentVersion:
        version = DocumentVersion(
            id=str(uuid.uuid4()),
            document_id=document.id,
            version_number=version_number,
            sha256=scanned.sha256,
            size_bytes=scanned.size_bytes,
            modified_at=scanned.modified_at,
            processing_status="registered" if scanned.supported else "unsupported",
            metadata_json={"folder_structure": list(scanned.folder_path)},
        )
        self.session.add(version)
        self.session.flush()
        document.current_version_id = version.id
        return version

    def pair_media_transcripts(
        self,
        registrations: list[RegistrationResult],
        transcript_not_required: set[str] | None = None,
    ) -> tuple[int, int, int, list[str]]:
        by_path = {item.scanned.relative_path.casefold(): item for item in registrations}
        not_required_paths = transcript_not_required or set()
        linked = 0
        missing = 0
        not_required = 0
        warnings: list[str] = []

        for item in registrations:
            if item.scanned.extension != ".mp4":
                continue
            video = item.document
            video.role = "media_asset"
            video.indexable = False
            video.processing_status = "media_asset"
            parent = PurePosixPath(item.scanned.relative_path).parent
            expected_name = f"Текстовый {PurePosixPath(item.scanned.file_name).stem}.docx"
            expected = (parent / expected_name).as_posix()
            transcript_item = by_path.get(expected.casefold())
            if transcript_item is not None:
                linked += 1
                transcript = transcript_item.document
                transcript.role = "transcript"
                transcript.indexable = True
                video.metadata_json = {
                    **video.metadata_json,
                    "transcript_status": "linked",
                    "transcript_document_id": transcript.id,
                }
                transcript.metadata_json = {
                    **transcript.metadata_json,
                    "media_document_id": video.id,
                    "is_transcript": True,
                }
                self._relation(video.id, transcript.id, "has_transcript")
                self._relation(transcript.id, video.id, "transcript_of")
                continue

            if item.scanned.relative_path.casefold() in not_required_paths:
                not_required += 1
                video.metadata_json = {
                    **video.metadata_json,
                    "transcript_status": "not_required",
                    "expected_transcript": expected,
                }
            else:
                missing += 1
                video.metadata_json = {
                    **video.metadata_json,
                    "transcript_status": "missing",
                    "expected_transcript": expected,
                }
                warnings.append(
                    f"MP4 transcript not found: {item.scanned.relative_path} (expected {expected_name})"
                )

        self.session.flush()
        return linked, missing, not_required, warnings

    def _relation(self, source: str, target: str, relation_type: str) -> None:
        existing = self.session.scalar(
            select(DocumentRelation).where(
                DocumentRelation.source_document_id == source,
                DocumentRelation.target_document_id == target,
                DocumentRelation.relation_type == relation_type,
            )
        )
        if existing is None:
            self.session.add(
                DocumentRelation(
                    id=str(uuid.uuid4()),
                    source_document_id=source,
                    target_document_id=target,
                    relation_type=relation_type,
                )
            )

    def relations_for(self, document_id: str) -> list[DocumentRelation]:
        return list(
            self.session.scalars(
                select(DocumentRelation).where(
                    or_(
                        DocumentRelation.source_document_id == document_id,
                        DocumentRelation.target_document_id == document_id,
                    )
                )
            )
        )
