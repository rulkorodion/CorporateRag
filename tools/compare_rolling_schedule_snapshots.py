from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from corporate_rag_ingestion.rolling_schedule.models import ScheduleRecord
from corporate_rag_ingestion.rolling_schedule.models import ScheduleQuery
from corporate_rag_ingestion.rolling_schedule.normalizer import normalize_workbook
from corporate_rag_ingestion.rolling_schedule.settings import (
    DEFAULT_EDIT_URL,
    DEFAULT_EXPORT_URL,
    SPREADSHEET_ID,
    RollingScheduleSettings,
)
from corporate_rag_ingestion.rolling_schedule.source import KNOWN_GIDS
from corporate_rag_ingestion.rolling_schedule.store import RollingScheduleStore


def _settings(root: Path) -> RollingScheduleSettings:
    return RollingScheduleSettings(
        enabled=True,
        root=root,
        export_url=DEFAULT_EXPORT_URL,
        edit_url=DEFAULT_EDIT_URL,
        spreadsheet_id=SPREADSHEET_ID,
        update_interval_hours=24,
        stale_after_hours=36,
        timeout_seconds=60,
        retries=3,
        max_download_bytes=100 * 1024 * 1024,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _key(record: ScheduleRecord) -> tuple[str, int, str]:
    return record.sheet_name, record.source_row, record.record_kind


def _comparable(record: ScheduleRecord) -> dict[str, Any]:
    value = record.to_dict()
    value.pop("record_id", None)
    value.pop("source_url", None)
    value.pop("gid", None)
    return value


def _summary(records: list[ScheduleRecord], *, today: date) -> dict[str, Any]:
    products = [item for item in records if item.record_kind == "product"]
    dated = [item for item in products if item.start_date is not None]
    current_or_future = [
        item
        for item in dated
        if (item.end_date or item.start_date) >= today  # type: ignore[operator]
    ]
    per_sheet = Counter(item.sheet_name for item in products)
    return {
        "products_total": len(products),
        "products_dated": len(dated),
        "products_undated": len(products) - len(dated),
        "products_current_or_future": len(current_or_future),
        "sitko_products": per_sheet.get("Ситко", 0),
        "products_by_sheet": dict(sorted(per_sheet.items())),
    }


def _date_bucket(record: ScheduleRecord, *, today: date) -> str:
    if record.start_date is None:
        return "undated"
    if (record.end_date or record.start_date) >= today:
        return "current_or_future"
    return "past"


def _load(path: Path, *, settings: RollingScheduleSettings) -> tuple[list[ScheduleRecord], str]:
    if path.suffix.casefold() in {".sqlite", ".sqlite3", ".db"}:
        store = RollingScheduleStore(path, path.with_suffix(".status.json"))
        records = store.search(
            ScheduleQuery(
                raw="snapshot comparison",
                is_schedule_intent=True,
                period_mode="all_available",
            )
        )
        return records, store.metadata().get("snapshot_sha256", _sha256(path))
    normalized = normalize_workbook(path, settings=settings, gid_map=dict(KNOWN_GIDS))
    return list(normalized.records), _sha256(path)


def compare(left: Path, right: Path, *, today: date) -> dict[str, Any]:
    settings = _settings(left.parent)
    left_records, left_sha256 = _load(left, settings=settings)
    right_records, right_sha256 = _load(right, settings=settings)
    left_by_key = {_key(item): item for item in left_records}
    right_by_key = {_key(item): item for item in right_records}

    left_only = sorted(set(left_by_key) - set(right_by_key))
    right_only = sorted(set(right_by_key) - set(left_by_key))
    changed: list[dict[str, Any]] = []
    for key in sorted(set(left_by_key) & set(right_by_key)):
        before = _comparable(left_by_key[key])
        after = _comparable(right_by_key[key])
        if before == after:
            continue
        fields = {
            name: {"left": before.get(name), "right": after.get(name)}
            for name in sorted(before.keys() | after.keys())
            if before.get(name) != after.get(name)
        }
        changed.append(
            {
                "sheet": key[0],
                "row": key[1],
                "record_kind": key[2],
                "fields": fields,
            }
        )

    left_products = [item for item in left_records if item.record_kind == "product"]
    right_products = [item for item in right_records if item.record_kind == "product"]
    left_names = Counter((item.sheet_name, item.product_name) for item in left_products)
    right_names = Counter((item.sheet_name, item.product_name) for item in right_products)
    product_name_differences: list[dict[str, Any]] = []
    for key in sorted(set(left_names) | set(right_names)):
        if left_names[key] == right_names[key]:
            continue
        product_name_differences.append(
            {
                "sheet": key[0],
                "product_name": key[1],
                "left_count": left_names[key],
                "right_count": right_names[key],
                "left_rows": [
                    item.source_row
                    for item in left_products
                    if (item.sheet_name, item.product_name) == key
                ],
                "right_rows": [
                    item.source_row
                    for item in right_products
                    if (item.sheet_name, item.product_name) == key
                ],
            }
        )
    date_bucket_transitions: list[dict[str, Any]] = []
    for key in sorted(set(left_by_key) & set(right_by_key)):
        before_record = left_by_key[key]
        after_record = right_by_key[key]
        if before_record.record_kind != "product" or after_record.record_kind != "product":
            continue
        before_bucket = _date_bucket(before_record, today=today)
        after_bucket = _date_bucket(after_record, today=today)
        if before_bucket == after_bucket:
            continue
        date_bucket_transitions.append(
            {
                "sheet": key[0],
                "row": key[1],
                "left_name": before_record.product_name,
                "right_name": after_record.product_name,
                "left_bucket": before_bucket,
                "right_bucket": after_bucket,
                "left_date_from": (
                    before_record.start_date.isoformat()
                    if before_record.start_date
                    else None
                ),
                "left_date_to": (
                    (before_record.end_date or before_record.start_date).isoformat()
                    if before_record.start_date
                    else None
                ),
                "right_date_from": (
                    after_record.start_date.isoformat()
                    if after_record.start_date
                    else None
                ),
                "right_date_to": (
                    (after_record.end_date or after_record.start_date).isoformat()
                    if after_record.start_date
                    else None
                ),
            }
        )

    return {
        "comparison_date": today.isoformat(),
        "left": {
            "path": str(left.resolve()),
            "sha256": left_sha256,
            **_summary(left_records, today=today),
        },
        "right": {
            "path": str(right.resolve()),
            "sha256": right_sha256,
            **_summary(right_records, today=today),
        },
        "differences": {
            "left_only": [
                {
                    "sheet": key[0],
                    "row": key[1],
                    "record_kind": key[2],
                    "record": _comparable(left_by_key[key]),
                }
                for key in left_only
            ],
            "right_only": [
                {
                    "sheet": key[0],
                    "row": key[1],
                    "record_kind": key[2],
                    "record": _comparable(right_by_key[key]),
                }
                for key in right_only
            ],
            "changed": changed,
            "product_name_count_differences": product_name_differences,
            "date_bucket_transitions": date_bucket_transitions,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare two rolling-schedule XLSX snapshots with one normalizer."
    )
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    payload = compare(arguments.left, arguments.right, today=arguments.today)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
