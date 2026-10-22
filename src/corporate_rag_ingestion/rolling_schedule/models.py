from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class ScheduleRecord:
    record_id: str
    sheet_name: str
    gid: str | None
    source_row: int
    record_kind: str
    start_date: date | None
    end_date: date | None
    product_name: str
    product_kind: str | None
    dimensions: tuple[str, ...]
    size_key: str | None
    diameter: str | None = None
    width: str | None = None
    height: str | None = None
    thickness: str | None = None
    length: str | None = None
    steel: str | None = None
    volume: str | None = None
    unit: str | None = None
    raw_date: str | None = None
    raw_volume: str | None = None
    uncertainties: tuple[str, ...] = ()
    source_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["start_date"] = self.start_date.isoformat() if self.start_date else None
        result["end_date"] = self.end_date.isoformat() if self.end_date else None
        result["dimensions"] = list(self.dimensions)
        result["uncertainties"] = list(self.uncertainties)
        return result


@dataclass(frozen=True)
class SheetDiagnostic:
    sheet_name: str
    gid: str | None
    status: str
    product_count: int
    service_count: int
    dated_record_count: int
    undated_record_count: int
    formula_error_count: int
    helper_issue_count: int
    formula_errors: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["warnings"] = list(self.warnings)
        return result


@dataclass(frozen=True)
class NormalizationResult:
    records: tuple[ScheduleRecord, ...]
    sheets: tuple[SheetDiagnostic, ...]
    workbook_sheet_count: int

    @property
    def product_count(self) -> int:
        return sum(item.product_count for item in self.sheets)

    @property
    def service_count(self) -> int:
        return sum(item.service_count for item in self.sheets)

    @property
    def formula_error_count(self) -> int:
        return sum(item.formula_error_count for item in self.sheets)


@dataclass(frozen=True)
class ScheduleQuery:
    raw: str
    is_schedule_intent: bool
    sheet_names: tuple[str, ...] = ()
    record_kinds: tuple[str, ...] = ()
    product_kinds: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    steel: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    date_explicit: bool = False
    period_mode: str = "default_future"
    include_undated: bool = False
    products_only: bool = False
    query_type: str = "summary"
    response_mode: str = "brief"
    page: int = 1
    needs_clarification: bool = False
    clarification_question: str | None = None


@dataclass(frozen=True)
class SyncReport:
    status: str
    started_at: datetime
    finished_at: datetime
    sha256: str | None = None
    downloaded_bytes: int = 0
    records: int = 0
    products: int = 0
    service_events: int = 0
    sheets: int = 0
    formula_errors: int = 0
    changed: bool = False
    skipped_reason: str | None = None
    error: str | None = None
    database_path: str | None = None
    snapshot_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("started_at", "finished_at"):
            value = result[key]
            result[key] = value.isoformat()
        return result
