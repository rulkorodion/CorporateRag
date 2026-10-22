from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal


PageVisualType = Literal["text", "mixed", "scan_candidate", "visual_or_slide", "ambiguous"]


@dataclass(frozen=True, slots=True)
class NativePdfPageAnalysis:
    page_number: int
    width: float
    height: float
    native_text: str
    native_text_char_count: int
    native_text_block_count: int
    native_word_count: int
    image_count: int
    largest_image_area_ratio: float
    image_coverage_area_ratio: float
    page_visual_type: PageVisualType
    needs_ocr: bool
    ocr_decision_reason: str


class PdfNativeAnalyzer:
    """Analyze the source PDF without OCR or Docling-derived text."""

    meaningful_image_area_ratio = 0.0005

    def analyze(self, path: Path) -> list[NativePdfPageAnalysis]:
        try:
            import pymupdf
        except ImportError as exc:  # pragma: no cover - dependency is declared by the project
            raise RuntimeError(
                "PyMuPDF is required for native PDF text-layer analysis. Install project dependencies."
            ) from exc

        # Broken embedded color profiles are common in old PDFs. MuPDF can still
        # inspect those pages, so suppress its native stderr noise and keep any
        # genuine Python exception as the actionable pipeline error.
        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)

        result: list[NativePdfPageAnalysis] = []
        document = pymupdf.open(path)
        try:
            for index, page in enumerate(document):
                native_text = self._clean_native_text(page.get_text("text", sort=True))
                chars = len(re.sub(r"\s+", "", native_text))
                words = len(page.get_text("words"))
                text_blocks = [
                    block
                    for block in page.get_text("blocks", sort=True)
                    if len(block) > 6
                    and block[6] == 0
                    and re.sub(r"\s+", "", str(block[4]))
                ]
                page_area = float(page.rect.width * page.rect.height)
                image_ratios = self._meaningful_image_ratios(page, page_area)
                largest_ratio = max(image_ratios, default=0.0)
                coverage_ratio = min(1.0, sum(image_ratios))
                visual_type, needs_ocr, reason = self.classify(
                    native_text_char_count=chars,
                    native_text_block_count=len(text_blocks),
                    native_word_count=words,
                    image_count=len(image_ratios),
                    largest_image_area_ratio=largest_ratio,
                    image_coverage_area_ratio=coverage_ratio,
                    width=float(page.rect.width),
                    height=float(page.rect.height),
                )
                result.append(
                    NativePdfPageAnalysis(
                        page_number=index + 1,
                        width=float(page.rect.width),
                        height=float(page.rect.height),
                        native_text=native_text,
                        native_text_char_count=chars,
                        native_text_block_count=len(text_blocks),
                        native_word_count=words,
                        image_count=len(image_ratios),
                        largest_image_area_ratio=round(largest_ratio, 6),
                        image_coverage_area_ratio=round(coverage_ratio, 6),
                        page_visual_type=visual_type,
                        needs_ocr=needs_ocr,
                        ocr_decision_reason=reason,
                    )
                )
        finally:
            document.close()
        return self._apply_document_context(result)

    @staticmethod
    def _clean_native_text(text: str) -> str:
        lines = [" ".join(line.split()) for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()

    @staticmethod
    def _apply_document_context(
        pages: list[NativePdfPageAnalysis],
    ) -> list[NativePdfPageAnalysis]:
        if len(pages) < 2:
            return pages
        page_count = len(pages)
        meaningful_types = {"text", "mixed", "visual_or_slide"}
        meaningful_count = sum(page.page_visual_type in meaningful_types for page in pages)
        scan_count = sum(page.page_visual_type == "scan_candidate" for page in pages)
        landscape_count = sum(
            page.height > 0 and page.width / page.height >= 1.2 for page in pages
        )
        presentation_document = (
            landscape_count / page_count >= 0.7
            and meaningful_count / page_count >= 0.4
        )
        predominantly_native_text_document = (
            page_count >= 5
            and meaningful_count / page_count >= 0.7
            and scan_count / page_count <= 0.1
        )
        adjusted: list[NativePdfPageAnalysis] = []
        for page in pages:
            if page.page_visual_type != "scan_candidate":
                adjusted.append(page)
            elif presentation_document:
                adjusted.append(
                    replace(
                        page,
                        page_visual_type="visual_or_slide",
                        needs_ocr=False,
                        ocr_decision_reason=(
                            "raster_only_page_within_native_text_presentation"
                        ),
                    )
                )
            elif predominantly_native_text_document:
                adjusted.append(
                    replace(
                        page,
                        page_visual_type="ambiguous",
                        needs_ocr=False,
                        ocr_decision_reason=(
                            "isolated_raster_page_within_predominantly_native_text_document"
                        ),
                    )
                )
            else:
                adjusted.append(page)
        return adjusted

    def _meaningful_image_ratios(self, page: object, page_area: float) -> list[float]:
        if page_area <= 0:
            return []
        ratios: list[float] = []
        seen: set[tuple[float, float, float, float, object]] = set()
        for image in page.get_image_info(xrefs=True):  # type: ignore[attr-defined]
            bbox = image.get("bbox")
            if not bbox:
                continue
            left, top, right, bottom = (float(value) for value in bbox)
            width = max(0.0, right - left)
            height = max(0.0, bottom - top)
            ratio = width * height / page_area
            if width < 4.0 or height < 4.0 or ratio < self.meaningful_image_area_ratio:
                continue
            digest = image.get("digest")
            key = (round(left, 2), round(top, 2), round(right, 2), round(bottom, 2), digest)
            if key in seen:
                continue
            seen.add(key)
            ratios.append(min(1.0, ratio))
        return ratios

    @staticmethod
    def classify(
        *,
        native_text_char_count: int,
        native_text_block_count: int,
        native_word_count: int,
        image_count: int,
        largest_image_area_ratio: float,
        image_coverage_area_ratio: float,
        width: float,
        height: float,
    ) -> tuple[PageVisualType, bool, str]:
        meaningful_text = (
            native_text_char_count >= 80
            or native_word_count >= 15
            or (native_text_char_count >= 40 and native_text_block_count >= 2)
        )
        landscape = height > 0 and width / height >= 1.2

        if meaningful_text:
            if landscape:
                return (
                    "visual_or_slide",
                    False,
                    "meaningful_native_text_on_landscape_slide_or_visual_page",
                )
            if image_count > 0:
                return "mixed", False, "meaningful_native_text_with_raster_images"
            return "text", False, "meaningful_native_text_layer"

        raster_dominant = (
            largest_image_area_ratio >= 0.72 or image_coverage_area_ratio >= 0.82
        )
        practically_no_text = native_text_char_count < 20 and native_word_count < 5
        if practically_no_text and image_count > 0 and raster_dominant:
            return (
                "scan_candidate",
                True,
                "practically_no_native_text_and_raster_dominates_page",
            )

        return (
            "ambiguous",
            False,
            "insufficient_evidence_for_text_or_scan_classification",
        )
