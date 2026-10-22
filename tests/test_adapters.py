from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from PIL import Image
from openpyxl import Workbook

from corporate_rag_ingestion.adapters import (
    AdapterContext,
    ExcelAdapter,
    ImageAdapter,
    InternetShortcutAdapter,
)
from corporate_rag_ingestion.chunking.chunker import StructuralChunker


def context_for(source: Path, kb: Path, storage: Path) -> AdapterContext:
    return AdapterContext(
        document_id=str(uuid.uuid4()),
        version_id=str(uuid.uuid4()),
        source_path=source,
        knowledge_base_root=kb,
        storage_root=storage,
        assets_directory=storage / "assets",
        metadata={"relative_path": source.relative_to(kb).as_posix()},
    )


@pytest.mark.parametrize(
    ("extension", "image_format"),
    [("jpg", "JPEG"), ("jpeg", "JPEG"), ("png", "PNG")],
)
def test_image_adapter_registers_dimensions_and_pending_status(
    tmp_path: Path, extension: str, image_format: str
) -> None:
    kb = tmp_path / "kb"
    storage = tmp_path / "storage"
    kb.mkdir()
    source = kb / f"photo.{extension}"
    Image.new("RGB", (31, 17), "red").save(source, image_format)

    normalized = ImageAdapter().normalize(context_for(source, kb, storage))

    assert normalized.metadata["width"] == 31
    assert normalized.metadata["height"] == 17
    assert normalized.metadata["visual_classification_status"] == "pending"
    assert normalized.assets[0].visual_classification_status == "pending"
    assert normalized.text == ""
    assert normalized.elements[0].metadata["searchable_title"] == "photo"


def test_url_adapter_detects_google_sheet_without_downloading(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    storage = tmp_path / "storage"
    kb.mkdir()
    source = kb / "plan.url"
    url = "https://docs.google.com/spreadsheets/d/abc/edit?gid=0"
    source.write_text(f"[InternetShortcut]\nURL={url}\n", encoding="utf-8")

    normalized = InternetShortcutAdapter().normalize(context_for(source, kb, storage))

    assert normalized.metadata["url"] == url
    assert normalized.metadata["external_resource_type"] == "external_google_sheet"
    assert normalized.metadata["download_status"] == "not_requested"


def test_excel_merged_values_are_propagated_to_each_semantic_row(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    storage = tmp_path / "storage"
    kb.mkdir()
    source = kb / "contacts.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Секретариат"
    sheet.append(["Раздел", "Тема обращения", "К кому обращаться"])
    sheet.append(["Командировки", "Билеты", "Бенько Мария Александровна"])
    sheet.append(["Канцелярия", "Заказать визитки", None])
    sheet.merge_cells("C2:C3")
    workbook.save(source)

    normalized = ExcelAdapter().normalize(context_for(source, kb, storage))
    chunked = StructuralChunker().chunk(normalized)

    assert normalized.elements[0].metadata["merged_cell_values"] == [
        {
            "range": "C2:C3",
            "min_row": 2,
            "max_row": 3,
            "min_column": 3,
            "max_column": 3,
            "value": "Бенько Мария Александровна",
        }
    ]
    assert "Тема обращения=Заказать визитки" in chunked.chunks[0].text
    assert "К кому обращаться=Бенько Мария Александровна" in chunked.chunks[0].text
