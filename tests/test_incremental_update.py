from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import portalocker
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from corporate_rag_ingestion.chunking.models import (
    ChunkCoverage,
    ChunkedDocument,
    RetrievalChunk,
)
from corporate_rag_ingestion.incremental.preparer import PreparedDocument
from corporate_rag_ingestion.incremental.service import IncrementalUpdater
from corporate_rag_ingestion.registry_models import Document, DocumentVersion
from corporate_rag_ingestion.retrieval.indexer import PreparedDocumentIndex
from corporate_rag_ingestion.retrieval.models import SparseEmbedding
from corporate_rag_ingestion.retrieval.store import DocumentSwitch


class FakePreparer:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_names: set[str] = set()
        self.ocr_release_calls = 0

    def prepare(self, _session, _registry, registration, **_kwargs):  # type: ignore[no-untyped-def]
        path = registration.scanned.relative_path
        self.calls.append(path)
        if registration.scanned.file_name in self.fail_names:
            raise RuntimeError("synthetic preparation failure")
        text = registration.scanned.absolute_path.read_bytes().decode("utf-8", errors="replace")
        chunk_id = str(uuid.uuid5(uuid.UUID(registration.document.id), registration.version.id))
        chunk = RetrievalChunk(
            chunk_id=chunk_id,
            document_id=registration.document.id,
            version_id=registration.version.id,
            relative_path=path,
            chunk_type="text",
            text=text or registration.scanned.file_name,
            token_count=1,
            source_element_ids=[f"{registration.version.id}:element:1"],
            source_type="ocr" if text.startswith("OCR:") else "native",
        )
        chunked = ChunkedDocument(
            document_id=registration.document.id,
            version_id=registration.version.id,
            relative_path=path,
            source_sha256=registration.scanned.sha256,
            chunker_version="test-incremental-v1",
            settings_hash="a" * 64,
            tokenizer_method="fake",
            chunks=[chunk],
            coverage=ChunkCoverage(
                eligible_source_elements=1,
                covered_source_elements=1,
            ),
        )
        return PreparedDocument(
            chunked=chunked,
            normalized=True,
            ocr_pages_processed=int(text.startswith("OCR:")),
        )

    def release_ocr(self) -> None:
        self.ocr_release_calls += 1


class FakeIndexer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def prepare_document(self, document: ChunkedDocument) -> PreparedDocumentIndex:
        self.calls.append(document.relative_path)
        records = [
            (
                chunk.chunk_id,
                [0.1],
                SparseEmbedding(indices=[1], values=[1.0]),
                {
                    "chunk_id": chunk.chunk_id,
                    "document_id": chunk.document_id,
                    "version_id": chunk.version_id,
                    "relative_path": chunk.relative_path,
                    "text": chunk.text,
                    "index_fingerprint": f"fp-{chunk.chunk_id}",
                },
            )
            for chunk in document.chunks
        ]
        return PreparedDocumentIndex(
            records=records,
            total_chunks=len(document.chunks),
            indexable_chunks=len(document.chunks),
            excluded_chunks=0,
            embedding_seconds=0.001,
            sparse_seconds=0.001,
        )


class FakeStore:
    def __init__(self) -> None:
        self.points: dict[str, dict[str, object]] = {}
        self.ensure_calls = 0
        self.metadata_calls = 0
        self.fail_document_ids: set[str] = set()

    def ensure_collection(self) -> bool:
        self.ensure_calls += 1
        return False

    def count(self) -> int:
        return len(self.points)

    def controlled_replace_document(self, document_id, version_id, records):  # type: ignore[no-untyped-def]
        if document_id in self.fail_document_ids:
            raise RuntimeError("synthetic Qdrant switch failure")
        previous = [
            SimpleNamespace(id=point_id, payload=dict(payload), vector={})
            for point_id, payload in self.points.items()
            if payload["document_id"] == document_id and payload["version_id"] != version_id
        ]
        for point in previous:
            self.points.pop(str(point.id), None)
        for point_id, _dense, _sparse, payload in records:
            self.points[point_id] = dict(payload)
        return DocumentSwitch(
            document_id=document_id,
            new_point_ids=[record[0] for record in records],
            previous_points=previous,  # type: ignore[arg-type]
        )

    def rollback_document_switch(self, switch: DocumentSwitch) -> None:
        for point_id in switch.new_point_ids:
            self.points.pop(point_id, None)
        for point in switch.previous_points:
            self.points[str(point.id)] = dict(point.payload or {})

    def remove_document(self, document_id: str) -> DocumentSwitch:
        previous = [
            SimpleNamespace(id=point_id, payload=dict(payload), vector={})
            for point_id, payload in list(self.points.items())
            if payload["document_id"] == document_id
        ]
        for point in previous:
            self.points.pop(str(point.id), None)
        return DocumentSwitch(
            document_id=document_id,
            new_point_ids=[],
            previous_points=previous,  # type: ignore[arg-type]
        )

    def set_index_metadata(self) -> None:
        self.metadata_calls += 1


def make_updater(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
) -> tuple[IncrementalUpdater, FakePreparer, FakeIndexer, FakeStore, Path]:
    knowledge_base = tmp_path / "knowledge-base"
    knowledge_base.mkdir(exist_ok=True)
    storage = tmp_path / "storage"
    preparer = FakePreparer()
    indexer = FakeIndexer()
    store = FakeStore()
    updater = IncrementalUpdater(
        knowledge_base_root=knowledge_base,
        storage_root=storage,
        session_factory=session_factory,
        preparer=preparer,  # type: ignore[arg-type]
        indexer=indexer,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
    )
    return updater, preparer, indexer, store, knowledge_base


def test_no_change_update_performs_zero_heavy_work(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
) -> None:
    updater, preparer, indexer, store, root = make_updater(tmp_path, session_factory)
    (root / "policy.docx").write_text("first policy", encoding="utf-8")
    first = updater.run()
    prepare_calls = len(preparer.calls)
    index_calls = len(indexer.calls)

    second = updater.run()

    assert first.new == 1 and first.embeddings_calculated == 1
    assert second.new == second.changed == second.deleted == 0
    assert second.unchanged == 1
    assert second.normalized == second.embeddings_calculated == 0
    assert second.embeddings_reused == 1
    assert len(preparer.calls) == prepare_calls
    assert len(indexer.calls) == index_calls
    assert len(store.points) == 1
    assert store.metadata_calls == 1


def test_add_change_delete_and_image_are_document_scoped(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
) -> None:
    updater, _preparer, _indexer, store, root = make_updater(tmp_path, session_factory)
    policy = root / "policy.docx"
    image = root / "factory.png"
    policy.write_text("old value", encoding="utf-8")
    updater.run()
    original_id = next(iter(store.points))

    image.write_text("image title", encoding="utf-8")
    added = updater.run()
    assert added.new == 1 and added.qdrant_points_added == 1
    assert len(store.points) == 2

    policy.write_text("new value", encoding="utf-8")
    changed = updater.run()
    assert changed.changed == 1
    assert changed.qdrant_points_added == changed.qdrant_points_removed == 1
    assert original_id not in store.points
    assert any(payload["text"] == "new value" for payload in store.points.values())
    assert any(payload["relative_path"] == "factory.png" for payload in store.points.values())

    image.unlink()
    deleted = updater.run()
    assert deleted.deleted == 1 and deleted.qdrant_points_removed == 1
    assert all(payload["relative_path"] != "factory.png" for payload in store.points.values())
    with session_factory() as session:
        image_document = session.scalar(
            select(Document).where(Document.relative_path == "factory.png")
        )
        assert image_document is not None
        assert image_document.processing_status == "deleted"
        assert session.scalars(
            select(DocumentVersion).where(DocumentVersion.document_id == image_document.id)
        ).all()

    repeated = updater.run()
    assert repeated.deleted == 0
    assert repeated.unchanged == 1
    assert repeated.qdrant_points_removed == 0


def test_failed_file_keeps_old_version_and_other_file_continues(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
) -> None:
    updater, preparer, _indexer, store, root = make_updater(tmp_path, session_factory)
    stable = root / "stable.docx"
    broken = root / "broken.docx"
    stable.write_text("stable v1", encoding="utf-8")
    broken.write_text("working v1", encoding="utf-8")
    updater.run()
    with session_factory() as session:
        broken_doc = session.scalar(select(Document).where(Document.relative_path == "broken.docx"))
        assert broken_doc is not None
        old_version = broken_doc.current_version_id

    preparer.fail_names.add("broken.docx")
    broken.write_text("broken v2", encoding="utf-8")
    stable.write_text("stable v2", encoding="utf-8")
    report = updater.run()

    assert report.changed == 2 and len(report.errors) == 1
    assert any(payload["text"] == "working v1" for payload in store.points.values())
    assert any(payload["text"] == "stable v2" for payload in store.points.values())
    with session_factory() as session:
        broken_doc = session.scalar(select(Document).where(Document.relative_path == "broken.docx"))
        assert broken_doc is not None
        assert broken_doc.current_version_id == old_version
        failed = session.scalars(
            select(DocumentVersion).where(DocumentVersion.document_id == broken_doc.id)
        ).all()
        assert any(version.processing_status == "error" for version in failed)


def test_qdrant_failure_keeps_active_version_and_old_points(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
) -> None:
    updater, _preparer, _indexer, store, root = make_updater(tmp_path, session_factory)
    source = root / "policy.docx"
    source.write_text("working v1", encoding="utf-8")
    updater.run()
    with session_factory() as session:
        document = session.scalar(select(Document).where(Document.relative_path == "policy.docx"))
        assert document is not None
        old_version = document.current_version_id
        store.fail_document_ids.add(document.id)

    source.write_text("candidate v2", encoding="utf-8")
    failed = updater.run()

    assert failed.changed == 1
    assert len(failed.errors) == 1
    assert {payload["text"] for payload in store.points.values()} == {"working v1"}
    with session_factory() as session:
        document = session.scalar(select(Document).where(Document.relative_path == "policy.docx"))
        assert document is not None
        assert document.current_version_id == old_version
        assert document.current_sha256 != next(
            version.sha256
            for version in session.scalars(
                select(DocumentVersion).where(DocumentVersion.document_id == document.id)
            )
            if version.processing_status == "error"
        )


def test_ocr_reporting_dry_run_force_and_repeat_are_idempotent(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
) -> None:
    updater, preparer, indexer, store, root = make_updater(tmp_path, session_factory)
    source = root / "scan.pdf"
    source.write_text("OCR: scanned page", encoding="utf-8")

    dry = updater.run(dry_run=True)
    assert dry.new == 1 and dry.normalized == dry.embeddings_calculated == 0
    assert not preparer.calls and not indexer.calls and not store.points
    assert not (tmp_path / "storage" / "incremental-update.lock").exists()
    assert not (tmp_path / "storage" / "reports").exists()

    first = updater.run()
    assert first.ocr_pages_processed == 1
    first_version = next(iter(store.points.values()))["version_id"]

    forced = updater.run(force=["scan.pdf"])
    assert forced.changed == 1 and forced.embeddings_calculated == 1
    assert next(iter(store.points.values()))["version_id"] != first_version

    repeat = updater.run()
    assert repeat.unchanged == 1
    assert repeat.embeddings_calculated == 0
    reports = list((tmp_path / "storage" / "reports" / "incremental").glob("*.json"))
    assert len(reports) == 3


def test_update_lock_and_unmatched_force_are_explicit(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
) -> None:
    updater, _preparer, _indexer, _store, _root = make_updater(tmp_path, session_factory)
    dry = updater.run(dry_run=True, force=["missing*.pdf"])
    assert dry.errors == ["force target did not match: missing*.pdf"]

    lock_path = tmp_path / "storage" / "incremental-update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(lock_path, mode="a", timeout=0):
        with pytest.raises(RuntimeError, match="already running"):
            updater.run()
