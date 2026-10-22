from __future__ import annotations

import fnmatch
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import portalocker
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..registry import DocumentRegistry, RegistrationResult, stable_document_id
from ..registry_models import Document, DocumentVersion
from ..retrieval.indexer import HybridIndexer
from ..retrieval.store import DocumentSwitch, QdrantStore
from ..scanner import FileScanner, ScannedFile
from .models import (
    FileUpdateResult,
    IncrementalPlan,
    IncrementalUpdateReport,
    PlannedFile,
)
from .preparer import IncrementalDocumentPreparer


class IncrementalUpdater:
    def __init__(
        self,
        *,
        knowledge_base_root: Path,
        storage_root: Path,
        session_factory: sessionmaker[Session],
        preparer: IncrementalDocumentPreparer,
        indexer: HybridIndexer,
        store: QdrantStore,
        scanner_factory: Callable[[Path], FileScanner] = FileScanner,
        reports_root: Path | None = None,
        monkeyocr_enabled: bool = False,
    ) -> None:
        self.knowledge_base_root = knowledge_base_root.resolve()
        self.storage_root = storage_root.resolve()
        self.session_factory = session_factory
        self.preparer = preparer
        self.indexer = indexer
        self.store = store
        self.scanner_factory = scanner_factory
        self.reports_root = (
            reports_root.resolve()
            if reports_root is not None
            else self.storage_root / "reports" / "incremental"
        )
        self.monkeyocr_enabled = monkeyocr_enabled

    def plan(self, *, force: list[str] | None = None) -> IncrementalPlan:
        scanned = self.scanner_factory(self.knowledge_base_root).scan()
        return self._plan(scanned, force=force or [])

    def run(
        self,
        *,
        dry_run: bool = False,
        force: list[str] | None = None,
    ) -> IncrementalUpdateReport:
        # A dry run is deliberately side-effect free: it does not create the
        # process lock, touch Qdrant, mutate the registry, or write a report.
        if dry_run:
            return self._run_locked(dry_run=True, force=force or [])
        lock_path = self.storage_root / "incremental-update.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with portalocker.Lock(lock_path, mode="a", timeout=0):
                return self._run_locked(dry_run=dry_run, force=force or [])
        except portalocker.exceptions.LockException as exc:
            raise RuntimeError("another incremental update is already running") from exc

    def _run_locked(
        self,
        *,
        dry_run: bool,
        force: list[str],
    ) -> IncrementalUpdateReport:
        started = time.perf_counter()
        scanned = self.scanner_factory(self.knowledge_base_root).scan()
        plan = self._plan(scanned, force=force)
        report = IncrementalUpdateReport(
            dry_run=dry_run,
            scanned=plan.scanned,
            new=len(plan.new),
            changed=len(plan.changed),
            deleted=len(plan.deleted),
            unchanged=len(plan.unchanged),
        )
        if dry_run:
            for item in [*plan.new, *plan.changed, *plan.deleted, *plan.unchanged]:
                report.add_file(
                    FileUpdateResult(
                        relative_path=item.relative_path,
                        state=item.state,
                        status="dry_run",
                        document_id=item.document_id,
                        previous_version_id=item.active_version_id,
                    )
                )
            report.elapsed_seconds = round(time.perf_counter() - started, 3)
            report.errors.extend(
                f"force target did not match: {target}" for target in plan.unmatched_force
            )
            return report

        self.store.ensure_collection()
        report.embeddings_reused = self.store.count()
        scanned_by_path = {item.relative_path: item for item in scanned}
        candidates = self._stage_candidates(plan, scanned)

        for item in [*plan.new, *plan.changed]:
            reference = candidates[item.relative_path]
            result = self._update_one(reference, scanned_by_path[item.relative_path], item)
            report.add_file(result)
            if result.status == "updated":
                report.embeddings_reused = max(
                    0, report.embeddings_reused - result.qdrant_points_removed
                )

        for item in plan.deleted:
            result = self._delete_one(item)
            report.add_file(result)
            if result.status == "deleted":
                report.embeddings_reused = max(
                    0, report.embeddings_reused - result.qdrant_points_removed
                )

        report.errors.extend(
            f"force target did not match: {target}" for target in plan.unmatched_force
        )
        # A no-change run performs read-only consistency checks only. In
        # particular, it does not PATCH collection metadata.
        if plan.new or plan.changed or plan.deleted:
            self.store.set_index_metadata()
        report.elapsed_seconds = round(time.perf_counter() - started, 3)
        self._save_report(report, plan)
        return report

    def _plan(self, scanned: list[ScannedFile], *, force: list[str]) -> IncrementalPlan:
        plan = IncrementalPlan(scanned=len(scanned))
        normalized_force = [item.replace("\\", "/") for item in force]
        matched_force: set[str] = set()
        with self.session_factory() as session:
            documents = list(session.scalars(select(Document)))
            by_path = {document.relative_path: document for document in documents}
            scanned_paths = {item.relative_path for item in scanned}
            for item in scanned:
                document = by_path.get(item.relative_path)
                forced = False
                if document is not None:
                    for pattern in normalized_force:
                        if (
                            pattern == document.id
                            or fnmatch.fnmatchcase(item.relative_path.casefold(), pattern.casefold())
                        ):
                            forced = True
                            matched_force.add(pattern)
                else:
                    for pattern in normalized_force:
                        if fnmatch.fnmatchcase(item.relative_path.casefold(), pattern.casefold()):
                            forced = True
                            matched_force.add(pattern)

                if document is None:
                    plan.new.append(
                        PlannedFile(
                            relative_path=item.relative_path,
                            state="new",
                            document_id=stable_document_id(item.relative_path),
                            candidate_sha256=item.sha256,
                            forced=forced,
                        )
                    )
                    continue
                pending = self._has_pending_candidate(session, document.id, item.sha256)
                changed = (
                    forced
                    or pending
                    or document.current_version_id is None
                    or document.current_sha256 != item.sha256
                    or document.processing_status == "deleted"
                )
                planned = PlannedFile(
                    relative_path=item.relative_path,
                    state="changed" if changed else "unchanged",
                    document_id=document.id,
                    active_version_id=document.current_version_id,
                    candidate_sha256=item.sha256,
                    forced=forced,
                )
                (plan.changed if changed else plan.unchanged).append(planned)

            for document in documents:
                if (
                    document.relative_path not in scanned_paths
                    and document.processing_status != "deleted"
                ):
                    plan.deleted.append(
                        PlannedFile(
                            relative_path=document.relative_path,
                            state="deleted",
                            document_id=document.id,
                            active_version_id=document.current_version_id,
                        )
                    )
        plan.unmatched_force = sorted(set(normalized_force) - matched_force)
        return plan

    @staticmethod
    def _has_pending_candidate(
        session: Session,
        document_id: str,
        sha256: str,
    ) -> bool:
        versions = session.scalars(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document_id,
                DocumentVersion.sha256 == sha256,
            )
        )
        return any(
            str(version.metadata_json.get("incremental_state") or "")
            in {"staged", "prepared", "index_switching"}
            for version in versions
        )

    def _stage_candidates(
        self,
        plan: IncrementalPlan,
        scanned: list[ScannedFile],
    ) -> dict[str, tuple[str, str]]:
        action_by_path = {
            item.relative_path: item for item in [*plan.new, *plan.changed]
        }
        staged: dict[str, tuple[str, str]] = {}
        with self.session_factory() as session:
            registry = DocumentRegistry(session)
            all_registrations: list[RegistrationResult] = []
            for scanned_file in scanned:
                action = action_by_path.get(scanned_file.relative_path)
                if action is not None:
                    registration = registry.stage(scanned_file, force=action.forced)
                    staged[scanned_file.relative_path] = (
                        registration.document.id,
                        registration.version.id,
                    )
                else:
                    document = session.scalar(
                        select(Document).where(
                            Document.relative_path == scanned_file.relative_path
                        )
                    )
                    if document is None or document.current_version_id is None:
                        continue
                    version = session.get(DocumentVersion, document.current_version_id)
                    if version is None:
                        continue
                    registration = RegistrationResult(
                        document=document,
                        version=version,
                        state="unchanged",
                        scanned=scanned_file,
                    )
                all_registrations.append(registration)
            registry.pair_media_transcripts(all_registrations)
            session.commit()
        return staged

    def _update_one(
        self,
        reference: tuple[str, str],
        scanned: ScannedFile,
        planned: PlannedFile,
    ) -> FileUpdateResult:
        started = time.perf_counter()
        document_id, version_id = reference
        switch: DocumentSwitch | None = None
        result = FileUpdateResult(
            relative_path=scanned.relative_path,
            state=planned.state,
            status="failed",
            document_id=document_id,
            previous_version_id=planned.active_version_id,
            version_id=version_id,
        )
        try:
            with self.session_factory() as session:
                document = session.get(Document, document_id)
                version = session.get(DocumentVersion, version_id)
                if document is None or version is None:
                    raise RuntimeError("staged registry candidate disappeared")
                previous_version = (
                    session.get(DocumentVersion, document.current_version_id)
                    if document.current_version_id
                    else None
                )
                registration = RegistrationResult(
                    document=document,
                    version=version,
                    state=planned.state,
                    scanned=scanned,
                )
                registry = DocumentRegistry(session)
                if scanned.supported:
                    prepared = self.preparer.prepare(
                        session,
                        registry,
                        registration,
                        previous_version=previous_version,
                        allow_monkeyocr=self.monkeyocr_enabled,
                    )
                    result.normalized = prepared.normalized
                    result.ocr_pages_processed = prepared.ocr_pages_processed
                    result.ocr_pages_reused = prepared.ocr_pages_reused
                    result.chunks_created = len(prepared.chunked.chunks)
                else:
                    prepared = None
                    version.processing_status = "unsupported"
                    version.metadata_json = {
                        **version.metadata_json,
                        "incremental_state": "prepared",
                    }
                session.commit()

                version.metadata_json = {
                    **version.metadata_json,
                    "incremental_state": "index_switching",
                }
                session.commit()

                if prepared is not None:
                    # MonkeyOCR and dense embedding share the same constrained
                    # production GPU. The candidate is already persisted, so
                    # release OCR weights before loading/calculating embeddings.
                    if prepared.ocr_pages_processed:
                        self.preparer.release_ocr()
                    indexed = self.indexer.prepare_document(prepared.chunked)
                    records = indexed.records
                    result.embeddings_calculated = indexed.indexable_chunks
                else:
                    records = []
                switch = self.store.controlled_replace_document(
                    document_id,
                    version_id,
                    records,
                )
                result.chunks_removed = len(switch.previous_points)
                result.qdrant_points_removed = len(switch.previous_points)
                result.qdrant_points_added = len(records)

                version.processing_status = "indexed" if scanned.supported else "unsupported"
                registry.promote(registration)
                try:
                    session.commit()
                except Exception:
                    self.store.rollback_document_switch(switch)
                    switch = None
                    raise
                result.status = "updated"
        except Exception as exc:
            if switch is not None:
                try:
                    self.store.rollback_document_switch(switch)
                except Exception as rollback_exc:
                    result.error = f"{type(exc).__name__}: {exc}; rollback failed: {rollback_exc}"
            if result.error is None:
                result.error = f"{type(exc).__name__}: {exc}"
            with self.session_factory() as failure_session:
                candidate = failure_session.get(DocumentVersion, version_id)
                if candidate is not None:
                    DocumentRegistry(failure_session).fail_candidate(candidate, result.error)
                    failure_session.commit()
        result.elapsed_seconds = round(time.perf_counter() - started, 3)
        return result

    def _delete_one(self, planned: PlannedFile) -> FileUpdateResult:
        started = time.perf_counter()
        result = FileUpdateResult(
            relative_path=planned.relative_path,
            state="deleted",
            status="failed",
            document_id=planned.document_id,
            previous_version_id=planned.active_version_id,
        )
        if planned.document_id is None:
            result.error = "deleted document has no document_id"
            return result
        switch: DocumentSwitch | None = None
        try:
            switch = self.store.remove_document(planned.document_id)
            with self.session_factory() as session:
                document = session.get(Document, planned.document_id)
                if document is None:
                    raise RuntimeError("registry document disappeared during deletion")
                DocumentRegistry(session).mark_deleted(document)
                try:
                    session.commit()
                except Exception:
                    self.store.rollback_document_switch(switch)
                    switch = None
                    raise
            result.status = "deleted"
            result.chunks_removed = len(switch.previous_points)
            result.qdrant_points_removed = len(switch.previous_points)
        except Exception as exc:
            if switch is not None:
                try:
                    self.store.rollback_document_switch(switch)
                except Exception as rollback_exc:
                    result.error = f"{type(exc).__name__}: {exc}; rollback failed: {rollback_exc}"
            if result.error is None:
                result.error = f"{type(exc).__name__}: {exc}"
        result.elapsed_seconds = round(time.perf_counter() - started, 3)
        return result

    def _save_report(
        self,
        report: IncrementalUpdateReport,
        plan: IncrementalPlan,
    ) -> None:
        self.reports_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        target = self.reports_root / f"incremental-update-{stamp}.json"
        report.report_path = str(target)
        payload = {
            "schema_version": "incremental-update-v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "plan": plan.to_dict(),
            "report": report.to_dict(),
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}-", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
