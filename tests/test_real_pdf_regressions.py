from __future__ import annotations

from pathlib import Path
import uuid

import pytest

from corporate_rag_ingestion.pdf_native import PdfNativeAnalyzer
from corporate_rag_ingestion.adapters import AdapterContext, DoclingAdapter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_BASE = PROJECT_ROOT / "База Знаний"


def find_one(pattern: str) -> Path:
    matches = list(KNOWLEDGE_BASE.rglob(pattern))
    if not matches:
        pytest.skip(f"Real regression fixture not available: {pattern}")
    return matches[0]


def test_real_outlook_instruction_is_native_text_with_screenshots() -> None:
    pages = PdfNativeAnalyzer().analyze(
        find_one("*Инструкция по настройке подписи в Outlook*.pdf")
    )
    assert pages
    assert all(page.native_text_char_count > 100 for page in pages)
    assert all(page.page_visual_type in {"text", "mixed"} for page in pages)
    assert all(page.needs_ocr is False for page in pages)
    assert sum(page.image_count for page in pages) > 0


def test_real_outlook_docling_keeps_screenshots_as_assets_without_ocr_text(
    tmp_path: Path,
) -> None:
    source = find_one("*Инструкция по настройке подписи в Outlook*.pdf")
    assets_directory = tmp_path / "assets"
    assets_directory.mkdir()
    context = AdapterContext(
        document_id=str(uuid.uuid4()),
        version_id=str(uuid.uuid4()),
        source_path=source,
        knowledge_base_root=KNOWLEDGE_BASE,
        storage_root=tmp_path,
        assets_directory=assets_directory,
        metadata={"relative_path": source.relative_to(KNOWLEDGE_BASE).as_posix()},
    )

    normalized = DoclingAdapter().normalize(context)
    expected_native_text = "\n\n".join(
        page.native_text.strip()
        for page in PdfNativeAnalyzer().analyze(source)
        if page.native_text.strip()
    )

    assert normalized.text == expected_native_text
    assert normalized.assets
    assert all((tmp_path / asset.local_path).is_file() for asset in normalized.assets)
    assert all(page.needs_ocr is False for page in normalized.pages)


def test_real_paper_scan_is_scan_candidate() -> None:
    pages = PdfNativeAnalyzer().analyze(find_one("Образец СЗ1.PDF"))
    assert len(pages) == 1
    assert pages[0].native_text_char_count == 0
    assert pages[0].page_visual_type == "scan_candidate"
    assert pages[0].needs_ocr is True


def test_real_accounting_presentation_uses_native_text_without_ocr() -> None:
    pages = PdfNativeAnalyzer().analyze(find_one("Бухгалтерия.2025*.pdf"))
    assert len(pages) == 12
    assert all(page.native_text_char_count > 0 for page in pages)
    assert all(page.page_visual_type in {"mixed", "visual_or_slide"} for page in pages)
    assert all(page.needs_ocr is False for page in pages)
    assert sum(page.image_count for page in pages) > 0
