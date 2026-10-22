from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import portalocker

from .models import SyncReport
from .normalizer import NORMALIZER_VERSION, ScheduleValidationError, normalize_workbook
from .settings import RollingScheduleSettings
from .source import KNOWN_GIDS, discover_gid_map, download_file
from .store import atomic_write_json, read_status, write_snapshot_database


LOGGER = logging.getLogger(__name__)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class RollingScheduleSyncService:
    def __init__(self, settings: RollingScheduleSettings) -> None:
        self.settings = settings

    def due(self, now: datetime | None = None) -> tuple[bool, str | None]:
        now = now or datetime.now(UTC)
        status = read_status(self.settings.status_path)
        last_success = _parse_time(status.get("last_successful_download_at"))
        if last_success is None or not self.settings.database_path.is_file():
            return True, None
        age = (now - last_success).total_seconds()
        if age < self.settings.update_interval_hours * 3600:
            return False, "successful snapshot is younger than update interval"
        return True, None

    def sync(
        self,
        *,
        force: bool = False,
        source_file: Path | None = None,
        gid_map: dict[str, str] | None = None,
    ) -> SyncReport:
        started = datetime.now(UTC)
        if not self.settings.enabled:
            return SyncReport(
                status="disabled",
                started_at=started,
                finished_at=datetime.now(UTC),
                skipped_reason="ROLLING_SCHEDULE_ENABLED is false",
            )
        self.settings.root.mkdir(parents=True, exist_ok=True)
        self.settings.snapshots_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.settings.root / ".sync.lock"
        try:
            with portalocker.Lock(lock_path, mode="a", timeout=1):
                return self._sync_locked(
                    started=started,
                    force=force,
                    source_file=source_file,
                    gid_map=gid_map,
                )
        except portalocker.exceptions.LockException:
            return SyncReport(
                status="skipped",
                started_at=started,
                finished_at=datetime.now(UTC),
                skipped_reason="another rolling schedule sync is active",
            )

    def _sync_locked(
        self,
        *,
        started: datetime,
        force: bool,
        source_file: Path | None,
        gid_map: dict[str, str] | None,
    ) -> SyncReport:
        previous_status = read_status(self.settings.status_path)
        due, reason = self.due(started)
        if not force and source_file is None and not due:
            return SyncReport(
                status="skipped",
                started_at=started,
                finished_at=datetime.now(UTC),
                sha256=previous_status.get("sha256"),
                skipped_reason=reason,
                database_path=str(self.settings.database_path),
            )
        with tempfile.TemporaryDirectory(
            prefix=".staging-", dir=self.settings.root
        ) as temporary_directory:
            staging = Path(temporary_directory)
            xlsx_path = staging / "schedule.xlsx"
            try:
                if source_file is not None:
                    if not source_file.is_file():
                        raise FileNotFoundError(source_file)
                    shutil.copyfile(source_file, xlsx_path)
                    downloaded_bytes = xlsx_path.stat().st_size
                else:
                    downloaded_bytes = download_file(
                        self.settings.export_url,
                        xlsx_path,
                        timeout_seconds=self.settings.timeout_seconds,
                        retries=self.settings.retries,
                        max_bytes=self.settings.max_download_bytes,
                    )
                source_hash = _sha256(xlsx_path)
                if (
                    source_hash == previous_status.get("sha256")
                    and previous_status.get("normalizer_version") == NORMALIZER_VERSION
                    and self.settings.database_path.is_file()
                ):
                    finished = datetime.now(UTC)
                    unchanged_status = {
                        **previous_status,
                        "status": "unchanged",
                        "last_attempt_at": finished.isoformat(),
                        "last_successful_download_at": finished.isoformat(),
                        "last_error": None,
                        "normalizer_version": NORMALIZER_VERSION,
                        "downloaded_bytes": downloaded_bytes,
                    }
                    atomic_write_json(self.settings.status_path, unchanged_status)
                    return SyncReport(
                        status="unchanged",
                        started_at=started,
                        finished_at=finished,
                        sha256=source_hash,
                        downloaded_bytes=downloaded_bytes,
                        records=int(previous_status.get("records", 0)),
                        products=int(previous_status.get("products", 0)),
                        service_events=int(previous_status.get("service_events", 0)),
                        sheets=int(previous_status.get("sheets", 0)),
                        formula_errors=int(previous_status.get("formula_errors", 0)),
                        changed=False,
                        skipped_reason="SHA-256 is unchanged",
                        database_path=str(self.settings.database_path),
                        snapshot_path=previous_status.get("snapshot_path"),
                    )
                active_gid_map = gid_map
                gid_warning: str | None = None
                if active_gid_map is None and source_file is None:
                    active_gid_map, gid_warning = discover_gid_map(
                        self.settings.edit_url,
                        timeout_seconds=self.settings.timeout_seconds,
                    )
                elif active_gid_map is None:
                    active_gid_map = dict(KNOWN_GIDS)
                normalized = normalize_workbook(
                    xlsx_path,
                    settings=self.settings,
                    gid_map=active_gid_map,
                )
                processed_at = datetime.now(UTC)
                staged_database = staging / "current.sqlite3"
                write_snapshot_database(
                    staged_database,
                    normalized=normalized,
                    metadata={
                        "spreadsheet_id": self.settings.spreadsheet_id,
                        "source_url": self.settings.edit_url,
                        "snapshot_sha256": source_hash,
                        "normalizer_version": NORMALIZER_VERSION,
                        "downloaded_at": processed_at.isoformat(),
                        "processed_at": processed_at.isoformat(),
                        "record_count": str(len(normalized.records)),
                        "product_count": str(normalized.product_count),
                        "service_count": str(normalized.service_count),
                    },
                )
                snapshot_path = self.settings.snapshots_root / f"{source_hash}.xlsx"
                if not snapshot_path.exists():
                    os.replace(xlsx_path, snapshot_path)
                os.replace(staged_database, self.settings.database_path)
                self._prune_snapshots(keep=2)
                status_payload = {
                    "status": "updated",
                    "sha256": source_hash,
                    "normalizer_version": NORMALIZER_VERSION,
                    "last_attempt_at": processed_at.isoformat(),
                    "last_successful_download_at": processed_at.isoformat(),
                    "last_successful_processing_at": processed_at.isoformat(),
                    "last_error": None,
                    "downloaded_bytes": downloaded_bytes,
                    "records": len(normalized.records),
                    "products": normalized.product_count,
                    "service_events": normalized.service_count,
                    "sheets": normalized.workbook_sheet_count,
                    "formula_errors": normalized.formula_error_count,
                    "snapshot_path": str(snapshot_path),
                    "database_path": str(self.settings.database_path),
                    "gid_discovery_warning": gid_warning,
                }
                atomic_write_json(self.settings.status_path, status_payload)
                report = SyncReport(
                    status="updated",
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    sha256=source_hash,
                    downloaded_bytes=downloaded_bytes,
                    records=len(normalized.records),
                    products=normalized.product_count,
                    service_events=normalized.service_count,
                    sheets=normalized.workbook_sheet_count,
                    formula_errors=normalized.formula_error_count,
                    changed=True,
                    database_path=str(self.settings.database_path),
                    snapshot_path=str(snapshot_path),
                )
                LOGGER.info("rolling schedule sync completed: %s", report.to_dict())
                return report
            except Exception as exc:
                finished = datetime.now(UTC)
                failure_status = {
                    **previous_status,
                    "status": "failed",
                    "last_attempt_at": finished.isoformat(),
                    "last_error": f"{type(exc).__name__}: {exc}",
                }
                atomic_write_json(self.settings.status_path, failure_status)
                LOGGER.exception("rolling schedule sync failed; active snapshot preserved")
                return SyncReport(
                    status="failed",
                    started_at=started,
                    finished_at=finished,
                    sha256=previous_status.get("sha256"),
                    downloaded_bytes=0,
                    records=int(previous_status.get("records", 0)),
                    products=int(previous_status.get("products", 0)),
                    service_events=int(previous_status.get("service_events", 0)),
                    sheets=int(previous_status.get("sheets", 0)),
                    formula_errors=int(previous_status.get("formula_errors", 0)),
                    changed=False,
                    error=f"{type(exc).__name__}: {exc}",
                    database_path=(
                        str(self.settings.database_path)
                        if self.settings.database_path.is_file()
                        else None
                    ),
                    snapshot_path=previous_status.get("snapshot_path"),
                )

    def _prune_snapshots(self, *, keep: int) -> None:
        snapshots = sorted(
            self.settings.snapshots_root.glob("*.xlsx"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for snapshot in snapshots[keep:]:
            snapshot.unlink()
