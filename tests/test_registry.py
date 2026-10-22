from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from corporate_rag_ingestion.registry import DocumentRegistry, RegistrationResult
from corporate_rag_ingestion.registry_models import Document, DocumentRelation, DocumentVersion
from corporate_rag_ingestion.scanner import FileScanner


def test_repeat_scan_is_idempotent_and_content_change_creates_version(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    root = tmp_path / "kb"
    root.mkdir()
    source = root / "policy.docx"
    source.write_bytes(b"version one")

    with session_factory() as session:
        first = DocumentRegistry(session).register(FileScanner(root).scan()[0])
        session.commit()
        document_id = first.document.id
        version_id = first.version.id
        assert first.state == "new"

    with session_factory() as session:
        second = DocumentRegistry(session).register(FileScanner(root).scan()[0])
        session.commit()
        assert second.state == "unchanged"
        assert second.document.id == document_id
        assert second.version.id == version_id
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 1

    source.write_bytes(b"version two")
    with session_factory() as session:
        third = DocumentRegistry(session).register(FileScanner(root).scan()[0])
        session.commit()
        assert third.state == "changed"
        assert third.document.id == document_id
        assert third.version.id != version_id
        assert third.version.version_number == 2
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 2


def test_mp4_is_paired_with_exact_sibling_transcript(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    root = tmp_path / "kb"
    nested = root / "training"
    nested.mkdir(parents=True)
    (nested / "video.mp4").write_bytes(b"video")
    (nested / "Текстовый video.docx").write_bytes(b"text")
    (nested / "orphan.mp4").write_bytes(b"orphan")

    with session_factory() as session:
        registry = DocumentRegistry(session)
        registrations = [registry.register(item) for item in FileScanner(root).scan()]
        linked, missing, not_required, warnings = registry.pair_media_transcripts(registrations)
        session.commit()

        assert (linked, missing, not_required) == (1, 1, 0)
        assert len(warnings) == 1
        documents = {doc.file_name: doc for doc in session.scalars(select(Document))}
        assert documents["video.mp4"].role == "media_asset"
        assert documents["video.mp4"].indexable is False
        assert documents["Текстовый video.docx"].role == "transcript"
        assert documents["Текстовый video.docx"].indexable is True
        assert session.scalar(select(func.count()).select_from(DocumentRelation)) == 2


def test_mp4_not_required_suppresses_warning(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    root = tmp_path / "kb"
    root.mkdir()
    (root / "silent.mp4").write_bytes(b"video")

    with session_factory() as session:
        registry = DocumentRegistry(session)
        registrations = [registry.register(item) for item in FileScanner(root).scan()]
        linked, missing, not_required, warnings = registry.pair_media_transcripts(
            registrations, transcript_not_required={"silent.mp4"}
        )
        session.commit()

        assert (linked, missing, not_required) == (0, 0, 1)
        assert warnings == []
        video = session.scalar(select(Document).where(Document.file_name == "silent.mp4"))
        assert video is not None
        assert video.metadata_json["transcript_status"] == "not_required"


def test_staged_version_does_not_replace_active_until_promoted(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    root = tmp_path / "kb"
    root.mkdir()
    source = root / "policy.docx"
    source.write_bytes(b"working version")
    with session_factory() as session:
        active = DocumentRegistry(session).register(FileScanner(root).scan()[0])
        session.commit()
        document_id = active.document.id
        active_version_id = active.version.id
        active_sha = active.scanned.sha256

    source.write_bytes(b"candidate version")
    scanned = FileScanner(root).scan()[0]
    with session_factory() as session:
        registry = DocumentRegistry(session)
        candidate = registry.stage(scanned)
        session.commit()
        candidate_version_id = candidate.version.id
        assert candidate_version_id != active_version_id
        assert candidate.document.current_version_id == active_version_id
        assert candidate.document.current_sha256 == active_sha

    with session_factory() as session:
        registry = DocumentRegistry(session)
        document = session.get(Document, document_id)
        version = session.get(DocumentVersion, candidate_version_id)
        assert document is not None and version is not None
        registry.promote(RegistrationResult(document, version, "changed", scanned))
        session.commit()
        assert document.current_version_id == version.id
        assert document.current_sha256 == scanned.sha256
        assert version.metadata_json["incremental_state"] == "active"
