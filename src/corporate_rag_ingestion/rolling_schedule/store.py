from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .models import NormalizationResult, ScheduleQuery, ScheduleRecord


SCHEMA_VERSION = "2"


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(
            f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro",
            uri=True,
        )
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def write_snapshot_database(
    path: Path,
    *,
    normalized: NormalizationResult,
    metadata: dict[str, str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE sheets (
                sheet_name TEXT PRIMARY KEY,
                gid TEXT,
                status TEXT NOT NULL,
                product_count INTEGER NOT NULL,
                service_count INTEGER NOT NULL,
                dated_record_count INTEGER NOT NULL,
                undated_record_count INTEGER NOT NULL,
                formula_error_count INTEGER NOT NULL,
                helper_issue_count INTEGER NOT NULL,
                formula_errors_json TEXT NOT NULL,
                warnings_json TEXT NOT NULL
            );
            CREATE TABLE records (
                record_id TEXT PRIMARY KEY,
                sheet_name TEXT NOT NULL,
                gid TEXT,
                source_row INTEGER NOT NULL,
                record_kind TEXT NOT NULL,
                start_date TEXT,
                end_date TEXT,
                product_name TEXT NOT NULL,
                product_kind TEXT,
                dimensions_json TEXT NOT NULL,
                size_key TEXT,
                diameter TEXT,
                width TEXT,
                height TEXT,
                thickness TEXT,
                length TEXT,
                steel TEXT,
                volume TEXT,
                unit TEXT,
                raw_date TEXT,
                raw_volume TEXT,
                uncertainties_json TEXT NOT NULL,
                source_url TEXT NOT NULL,
                FOREIGN KEY(sheet_name) REFERENCES sheets(sheet_name)
            );
            CREATE INDEX idx_records_sheet ON records(sheet_name);
            CREATE INDEX idx_records_date ON records(start_date, end_date);
            CREATE INDEX idx_records_kind_size ON records(product_kind, size_key);
            """
        )
        values = {"schema_version": SCHEMA_VERSION, **metadata}
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)", values.items()
        )
        connection.executemany(
            """
            INSERT INTO sheets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.sheet_name,
                    item.gid,
                    item.status,
                    item.product_count,
                    item.service_count,
                    item.dated_record_count,
                    item.undated_record_count,
                    item.formula_error_count,
                    item.helper_issue_count,
                    json.dumps(item.formula_errors, ensure_ascii=False),
                    json.dumps(item.warnings, ensure_ascii=False),
                )
                for item in normalized.sheets
            ],
        )
        connection.executemany(
            """
            INSERT INTO records VALUES (
                :record_id, :sheet_name, :gid, :source_row, :record_kind,
                :start_date, :end_date, :product_name, :product_kind,
                :dimensions_json, :size_key, :diameter, :width, :height,
                :thickness, :length, :steel, :volume, :unit, :raw_date,
                :raw_volume, :uncertainties_json, :source_url
            )
            """,
            [
                {
                    **item.to_dict(),
                    "dimensions_json": json.dumps(
                        item.dimensions, ensure_ascii=False
                    ),
                    "uncertainties_json": json.dumps(
                        item.uncertainties, ensure_ascii=False
                    ),
                }
                for item in normalized.records
            ],
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(FULL)")
    finally:
        connection.close()
    with path.open("rb+") as handle:
        os.fsync(handle.fileno())


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_status(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class StoredSheetDiagnostic:
    sheet_name: str
    gid: str | None
    status: str
    product_count: int
    service_count: int
    dated_record_count: int
    undated_record_count: int
    formula_error_count: int
    helper_issue_count: int
    formula_errors: dict[str, int]
    warnings: tuple[str, ...]


class RollingScheduleStore:
    def __init__(self, database_path: Path, status_path: Path) -> None:
        self.database_path = database_path
        self.status_path = status_path

    @property
    def available(self) -> bool:
        return self.database_path.is_file()

    def metadata(self) -> dict[str, str]:
        if not self.available:
            return {}
        try:
            with _connect(self.database_path, read_only=True) as connection:
                return {
                    row["key"]: row["value"]
                    for row in connection.execute("SELECT key, value FROM metadata")
                }
        except sqlite3.Error:
            return {}

    def status(self) -> dict[str, Any]:
        return read_status(self.status_path)

    def sheet_diagnostics(
        self, sheet_names: tuple[str, ...] = ()
    ) -> list[StoredSheetDiagnostic]:
        if not self.available:
            return []
        sql = (
            "SELECT sheet_name, gid, status, product_count, service_count, "
            "dated_record_count, undated_record_count, formula_error_count, "
            "helper_issue_count, formula_errors_json, warnings_json FROM sheets"
        )
        parameters: list[str] = []
        if sheet_names:
            sql += " WHERE sheet_name IN (" + ",".join("?" for _ in sheet_names) + ")"
            parameters.extend(sheet_names)
        sql += " ORDER BY sheet_name"
        with _connect(self.database_path, read_only=True) as connection:
            return [
                StoredSheetDiagnostic(
                    sheet_name=row["sheet_name"],
                    gid=row["gid"],
                    status=row["status"],
                    product_count=row["product_count"],
                    service_count=row["service_count"],
                    dated_record_count=row["dated_record_count"],
                    undated_record_count=row["undated_record_count"],
                    formula_error_count=row["formula_error_count"],
                    helper_issue_count=row["helper_issue_count"],
                    formula_errors=json.loads(row["formula_errors_json"]),
                    warnings=tuple(json.loads(row["warnings_json"])),
                )
                for row in connection.execute(sql, parameters)
            ]

    def search(self, query: ScheduleQuery) -> list[ScheduleRecord]:
        if not self.available:
            return []
        clauses = ["1=1"]
        parameters: list[str] = []
        if query.sheet_names:
            clauses.append(
                "sheet_name IN (" + ",".join("?" for _ in query.sheet_names) + ")"
            )
            parameters.extend(query.sheet_names)
        if query.products_only:
            clauses.append("record_kind = 'product'")
        if query.record_kinds:
            clauses.append(
                "record_kind IN (" + ",".join("?" for _ in query.record_kinds) + ")"
            )
            parameters.extend(query.record_kinds)
        if query.product_kinds:
            clauses.append(
                "product_kind IN (" + ",".join("?" for _ in query.product_kinds) + ")"
            )
            parameters.extend(query.product_kinds)
        if query.steel:
            clauses.append("LOWER(COALESCE(steel, '')) = LOWER(?)")
            parameters.append(query.steel)
        if query.date_from:
            if query.include_undated:
                clauses.append(
                    "(start_date IS NULL OR COALESCE(end_date, start_date) >= ?)"
                )
            else:
                clauses.append("start_date IS NOT NULL")
                clauses.append("COALESCE(end_date, start_date) >= ?")
            parameters.append(query.date_from.isoformat())
        if query.date_to:
            clauses.append("start_date <= ?")
            parameters.append(query.date_to.isoformat())
        sql = "SELECT * FROM records WHERE " + " AND ".join(clauses)
        sql += " ORDER BY COALESCE(start_date, '9999-12-31'), sheet_name, source_row"
        with _connect(self.database_path, read_only=True) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        records = [self._record(row) for row in rows]
        if query.dimensions:
            requested = tuple(query.dimensions)
            exact: list[ScheduleRecord] = []
            for record in records:
                size = tuple((record.size_key or "").split("x")) if record.size_key else ()
                if requested == size or requested == record.dimensions:
                    exact.append(record)
            records = exact
        # Only collapse byte-for-byte technical duplicates of the same source
        # row. Identical products on different rows remain separate plan items.
        unique: list[ScheduleRecord] = []
        seen: set[tuple[object, ...]] = set()
        for record in records:
            identity = (
                record.sheet_name,
                record.gid,
                record.source_row,
                record.record_kind,
                record.start_date,
                record.end_date,
                record.product_name,
                record.volume,
                record.unit,
            )
            if identity not in seen:
                seen.add(identity)
                unique.append(record)
        return unique

    @staticmethod
    def _record(row: sqlite3.Row) -> ScheduleRecord:
        return ScheduleRecord(
            record_id=row["record_id"],
            sheet_name=row["sheet_name"],
            gid=row["gid"],
            source_row=row["source_row"],
            record_kind=row["record_kind"],
            start_date=date.fromisoformat(row["start_date"]) if row["start_date"] else None,
            end_date=date.fromisoformat(row["end_date"]) if row["end_date"] else None,
            product_name=row["product_name"],
            product_kind=row["product_kind"],
            dimensions=tuple(json.loads(row["dimensions_json"])),
            size_key=row["size_key"],
            diameter=row["diameter"],
            width=row["width"],
            height=row["height"],
            thickness=row["thickness"],
            length=row["length"],
            steel=row["steel"],
            volume=row["volume"],
            unit=row["unit"],
            raw_date=row["raw_date"],
            raw_volume=row["raw_volume"],
            uncertainties=tuple(json.loads(row["uncertainties_json"])),
            source_url=row["source_url"],
        )

    def is_stale(self, stale_after_hours: int) -> bool:
        status = self.status()
        metadata = self.metadata()
        timestamp = status.get("last_successful_download_at") or metadata.get(
            "processed_at"
        )
        if not timestamp:
            return True
        try:
            processed = datetime.fromisoformat(timestamp)
            if processed.tzinfo is None:
                processed = processed.replace(tzinfo=UTC)
        except ValueError:
            return True
        return (datetime.now(UTC) - processed).total_seconds() > stale_after_hours * 3600
