from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pymupdf
from PIL import Image

from corporate_rag_ingestion.pdf_native import PdfNativeAnalyzer


LONG_TEXT = (
    "This is meaningful native text for a corporate instruction page. "
    "It contains enough words and characters to represent searchable content."
)


def image_bytes(width: int = 1200, height: int = 1600) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (width, height), "white").save(stream, "PNG")
    return stream.getvalue()


def save_pdf(document: pymupdf.Document, path: Path) -> Path:
    document.save(path)
    document.close()
    return path


def test_native_text_with_screenshot_is_mixed_and_not_ocr(tmp_path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_textbox(pymupdf.Rect(40, 40, 555, 180), LONG_TEXT, fontsize=12)
    page.insert_image(pymupdf.Rect(70, 250, 525, 600), stream=image_bytes(800, 600))

    analysis = PdfNativeAnalyzer().analyze(save_pdf(document, tmp_path / "mixed.pdf"))[0]

    assert analysis.native_text_char_count > 80
    assert analysis.image_count == 1
    assert analysis.page_visual_type == "mixed"
    assert analysis.needs_ocr is False


def test_raster_scan_without_native_text_needs_ocr(tmp_path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=image_bytes())

    analysis = PdfNativeAnalyzer().analyze(save_pdf(document, tmp_path / "scan.pdf"))[0]

    assert analysis.native_text_char_count == 0
    assert analysis.largest_image_area_ratio > 0.9
    assert analysis.page_visual_type == "scan_candidate"
    assert analysis.needs_ocr is True


def test_presentation_with_native_text_and_screenshot_is_not_ocr(tmp_path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=842, height=595)
    page.insert_textbox(pymupdf.Rect(40, 40, 800, 160), LONG_TEXT, fontsize=14)
    page.insert_image(pymupdf.Rect(450, 220, 800, 500), stream=image_bytes(800, 600))

    analysis = PdfNativeAnalyzer().analyze(save_pdf(document, tmp_path / "slides.pdf"))[0]

    assert analysis.page_visual_type == "visual_or_slide"
    assert analysis.needs_ocr is False


def test_raster_title_slide_in_native_text_presentation_is_not_scan_candidate(
    tmp_path: Path,
) -> None:
    document = pymupdf.open()
    title = document.new_page(width=842, height=595)
    title.insert_image(title.rect, stream=image_bytes(1600, 900))
    content = document.new_page(width=842, height=595)
    content.insert_textbox(pymupdf.Rect(40, 40, 800, 200), LONG_TEXT, fontsize=14)

    pages = PdfNativeAnalyzer().analyze(save_pdf(document, tmp_path / "deck.pdf"))

    assert pages[0].page_visual_type == "visual_or_slide"
    assert pages[0].needs_ocr is False
    assert "presentation" in pages[0].ocr_decision_reason


def test_insufficient_signals_are_ambiguous_without_ocr(tmp_path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((40, 40), "Short label", fontsize=10)

    analysis = PdfNativeAnalyzer().analyze(save_pdf(document, tmp_path / "ambiguous.pdf"))[0]

    assert analysis.page_visual_type == "ambiguous"
    assert analysis.needs_ocr is False
    assert "insufficient_evidence" in analysis.ocr_decision_reason


def test_native_text_whitespace_is_normalized() -> None:
    assert PdfNativeAnalyzer._clean_native_text(" Title \n\n  first   line \n second\tline ") == (
        "Title\nfirst line\nsecond line"
    )
