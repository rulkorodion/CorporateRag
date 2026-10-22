from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


SPREADSHEET_ID = "REPLACE_WITH_SPREADSHEET_ID"
DEFAULT_EXPORT_URL = (
    f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/export?format=xlsx"
)
DEFAULT_EDIT_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"

EXPECTED_SHEETS = (
    "159",
    "Королев",
    "Кореец",
    "ЭЗТМ",
    "51",
    "89",
    "250",
    "ПИ",
    "Ситко",
    "Элмаксан",
    "Адафер",
    "Китаец",
    "Белорус",
    "Малтибест",
    "114",
    "76",
    "127ТМ",
    "76ТМ",
    "Челябинск",
    "El_Череп",
    "Лист ДМЦ ГК",
    "Лист ДМЦ ХК",
    "Ижевск ХК",
    "Ижевск ХК 2",
    "Ижевск ХК 3",
    "Стан_ХК НСК",
    "Стан_ГК НСК",
    "ГК ВЛГ",
    "ХК ВЛГ",
)


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RollingScheduleSettings:
    enabled: bool
    root: Path
    export_url: str
    edit_url: str
    spreadsheet_id: str
    update_interval_hours: int
    stale_after_hours: int
    timeout_seconds: float
    retries: int
    max_download_bytes: int
    expected_sheets: tuple[str, ...] = EXPECTED_SHEETS

    @classmethod
    def from_environment(cls, storage_root: Path) -> "RollingScheduleSettings":
        root = Path(
            os.getenv(
                "ROLLING_SCHEDULE_ROOT",
                str(storage_root / "rolling-schedules"),
            )
        )
        return cls(
            enabled=_bool_env("ROLLING_SCHEDULE_ENABLED", True),
            root=root,
            export_url=os.getenv("ROLLING_SCHEDULE_URL", DEFAULT_EXPORT_URL).strip(),
            edit_url=os.getenv("ROLLING_SCHEDULE_EDIT_URL", DEFAULT_EDIT_URL).strip(),
            spreadsheet_id=os.getenv(
                "ROLLING_SCHEDULE_SPREADSHEET_ID", SPREADSHEET_ID
            ).strip(),
            update_interval_hours=max(
                1, int(os.getenv("ROLLING_SCHEDULE_UPDATE_INTERVAL_HOURS", "24"))
            ),
            stale_after_hours=max(
                1, int(os.getenv("ROLLING_SCHEDULE_STALE_AFTER_HOURS", "36"))
            ),
            timeout_seconds=max(
                5.0, float(os.getenv("ROLLING_SCHEDULE_TIMEOUT_SECONDS", "60"))
            ),
            retries=max(1, int(os.getenv("ROLLING_SCHEDULE_DOWNLOAD_RETRIES", "3"))),
            max_download_bytes=max(
                1_000_000,
                int(os.getenv("ROLLING_SCHEDULE_MAX_DOWNLOAD_BYTES", "104857600")),
            ),
        )

    @property
    def database_path(self) -> Path:
        return self.root / "current.sqlite3"

    @property
    def status_path(self) -> Path:
        return self.root / "status.json"

    @property
    def snapshots_root(self) -> Path:
        return self.root / "snapshots"
