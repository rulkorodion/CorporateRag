from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from ..pdf_native import NativePdfPageAnalysis, PdfNativeAnalyzer
from ..schemas import (
    BoundingBox,
    NormalizedAsset,
    NormalizedElement,
    NormalizedPage,
    NormalizedRelation,
)
from .base import AdapterContext, FormatAdapter


LOGGER = logging.getLogger(__name__)


def _bbox_from_provenance(provenance: Any) -> BoundingBox | None:
    bbox = getattr(provenance, "bbox", None)
    if bbox is None:
        return None
    origin = getattr(bbox, "coord_origin", None)
    if hasattr(origin, "value"):
        origin = origin.value
    return BoundingBox(
        left=float(bbox.l),
        top=float(bbox.t),
        right=float(bbox.r),
        bottom=float(bbox.b),
        coordinate_origin=str(origin) if origin is not None else None,
    )


def _item_ref(item: Any, fallback: str) -> str:
    ref = getattr(item, "self_ref", None)
    return str(ref) if ref is not None else fallback


def _ref_value(ref: Any) -> str | None:
    if ref is None:
        return None
    value = getattr(ref, "cref", ref)
    return str(value) if value is not None else None


class DoclingAdapter(FormatAdapter):
    """Docling boundary adapter. OCR is deliberately disabled for this stage."""

    def __init__(
        self,
        document_timeout_seconds: float = 300.0,
        pdf_native_analyzer: PdfNativeAnalyzer | None = None,
        artifacts_path: Path | None = None,
    ) -> None:
        self._converter: Any | None = None
        self._document_timeout_seconds = document_timeout_seconds
        self._pdf_native_analyzer = pdf_native_analyzer or PdfNativeAnalyzer()
        self._artifacts_path = artifacts_path

    def _get_converter(self):  # type: ignore[no-untyped-def]
        if self._converter is None:
            try:
                from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import PdfPipelineOptions
                from docling.document_converter import DocumentConverter, PdfFormatOption
            except ImportError as exc:
                raise RuntimeError(
                    "Docling is required for PDF/DOCX normalization. Install project dependencies."
                ) from exc

            options = PdfPipelineOptions()
            options.document_timeout = self._document_timeout_seconds
            if self._artifacts_path is not None:
                if not self._artifacts_path.is_dir():
                    raise RuntimeError(
                        f"Docling artifacts directory is unavailable: {self._artifacts_path}"
                    )
                options.artifacts_path = self._artifacts_path
            options.do_ocr = False
            options.do_table_structure = True
            options.generate_picture_images = True
            options.generate_page_images = False
            options.do_picture_classification = False
            options.do_picture_description = False
            # torch.compile requires a local C++ toolchain on Windows; eager inference is
            # deterministic and avoids making Visual Studio a runtime dependency.
            options.layout_options.engine_options.compile_model = False
            self._converter = DocumentConverter(
                allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
                format_options={
                    InputFormat.PDF: PdfFormatOption(
                        pipeline_options=options,
                        backend=PyPdfiumDocumentBackend,
                    )
                },
            )
        return self._converter

    def normalize(self, context: AdapterContext):  # type: ignore[no-untyped-def]
        native_pages = (
            self._pdf_native_analyzer.analyze(context.source_path)
            if context.source_path.suffix.casefold() == ".pdf"
            else []
        )
        conversion = self._get_converter().convert(context.source_path)
        document = conversion.document
        pages = self._pages(document, native_pages)
        page_by_number = {page.page_number: page for page in pages}
        elements: list[NormalizedElement] = []
        assets: list[NormalizedAsset] = []
        relations: list[NormalizedRelation] = []
        main_text_parts: list[str] = []

        for docling_order, (item, _level) in enumerate(document.iterate_items()):
            label_obj = getattr(item, "label", "text")
            label = str(getattr(label_obj, "value", label_obj)).casefold()
            element_type = self._element_type(label)
            if element_type is None:
                continue
            provenance = item.prov[0] if getattr(item, "prov", None) else None
            page_number = int(provenance.page_no) if provenance is not None else None
            bbox = _bbox_from_provenance(provenance) if provenance is not None else None
            ref = _item_ref(item, f"item-{docling_order}")
            element_id = str(uuid.uuid5(uuid.UUID(context.document_id), f"{context.version_id}:{ref}"))
            parent_ref = _ref_value(getattr(item, "parent", None))
            parent_id = (
                str(uuid.uuid5(uuid.UUID(context.document_id), f"{context.version_id}:{parent_ref}"))
                if parent_ref
                else None
            )
            metadata: dict[str, Any] = {
                "docling_label": label,
                "docling_ref": ref,
                "docling_order": docling_order,
            }
            text: str | None = getattr(item, "text", None)

            if element_type == "table":
                try:
                    text = item.export_to_markdown(document)
                    frame = item.export_to_dataframe(document)
                    metadata["rows"] = [
                        {str(key): _json_safe(value) for key, value in row.items()}
                        for row in frame.to_dict(orient="records")
                    ]
                    metadata["columns"] = [str(column) for column in frame.columns]
                except Exception as exc:  # table text is still useful if structured export fails
                    LOGGER.warning("Could not export Docling table %s: %s", ref, exc)
            elif element_type == "image":
                asset = self._save_picture(
                    context, document, item, docling_order, page_number, bbox
                )
                if asset is not None:
                    assets.append(asset)
                    metadata["asset_id"] = asset.asset_id
                text = None  # never leak OCR/captioned image text into the main document text

            reading_order = len(elements)
            element = NormalizedElement(
                element_id=element_id,
                type=element_type,
                text=text,
                page_number=page_number,
                bbox=bbox,
                order=reading_order,
                reading_order=reading_order,
                parent_id=parent_id,
                metadata=metadata,
            )
            elements.append(element)
            if text and element_type != "image":
                main_text_parts.append(text)
            if parent_id:
                relations.append(
                    NormalizedRelation(source_id=element_id, target_id=parent_id, type="parent")
                )
            self._update_page_metrics(page_by_number.get(page_number), element)

        self._link_reading_order(elements, relations)
        self._link_images_to_nearby_text(elements, assets, relations)
        self._apply_native_analysis(page_by_number, native_pages)

        normalized_text = "\n\n".join(part.strip() for part in main_text_parts if part.strip())
        if native_pages:
            # The searchable PDF text must be the source PDF's native layer. OCR is
            # disabled, so embedded screenshots cannot leak into this field.
            normalized_text = "\n\n".join(
                page.native_text.strip() for page in native_pages if page.native_text.strip()
            )

        return context.normalized_document(
            metadata={
                **context.metadata,
                "parser": "docling",
                "pdf_native_analyzer": "pymupdf" if native_pages else None,
                "ocr_executed": False,
            },
            text=normalized_text,
            pages=pages,
            elements=elements,
            assets=assets,
            relations=relations,
        )

    def upgrade_existing(
        self, context: AdapterContext, normalized: Any
    ):  # type: ignore[no-untyped-def]
        """Refresh derived schema fields without rerunning Docling on unchanged content."""
        normalized.metadata = {**normalized.metadata, **context.metadata}
        native_pages = (
            self._pdf_native_analyzer.analyze(context.source_path)
            if context.source_path.suffix.casefold() == ".pdf"
            else []
        )
        if native_pages:
            page_by_number = {page.page_number: page for page in normalized.pages}
            for native in native_pages:
                page_by_number.setdefault(
                    native.page_number,
                    NormalizedPage(
                        page_number=native.page_number,
                        width=native.width,
                        height=native.height,
                    ),
                )
            normalized.pages = [page_by_number[number] for number in sorted(page_by_number)]
            self._apply_native_analysis(page_by_number, native_pages)
            normalized.text = "\n\n".join(
                page.native_text.strip() for page in native_pages if page.native_text.strip()
            )
            normalized.metadata["pdf_native_analyzer"] = "pymupdf"
            normalized.metadata["ocr_executed"] = False

        for reading_order, element in enumerate(normalized.elements):
            element.order = reading_order
            element.reading_order = reading_order
            element.previous_id = None
            element.next_id = None
        normalized.relations = [
            relation
            for relation in normalized.relations
            if relation.type not in {"previous", "next"}
            and not (
                relation.type == "related_to"
                and relation.metadata.get("basis") == "same_page_immediate_reading_order"
            )
        ]
        for asset in normalized.assets:
            asset.nearby_text_element_ids = []
        self._link_reading_order(normalized.elements, normalized.relations)
        self._link_images_to_nearby_text(
            normalized.elements, normalized.assets, normalized.relations
        )
        return normalized

    @staticmethod
    def _element_type(label: str) -> str | None:
        mapping = {
            "title": "heading",
            "section_header": "heading",
            "text": "paragraph",
            "paragraph": "paragraph",
            "list_item": "list",
            "table": "table",
            "picture": "image",
            "caption": "paragraph",
            "page_header": "paragraph",
            "page_footer": "paragraph",
            "footnote": "paragraph",
        }
        return mapping.get(label)

    @staticmethod
    def _pages(
        document: Any, native_pages: list[NativePdfPageAnalysis]
    ) -> list[NormalizedPage]:
        pages_by_number: dict[int, NormalizedPage] = {}
        for number, page in sorted(document.pages.items()):
            size = getattr(page, "size", None)
            pages_by_number[int(number)] = NormalizedPage(
                page_number=int(number),
                width=float(size.width) if size is not None else None,
                height=float(size.height) if size is not None else None,
            )
        for native in native_pages:
            pages_by_number.setdefault(
                native.page_number,
                NormalizedPage(
                    page_number=native.page_number,
                    width=native.width,
                    height=native.height,
                ),
            )
        return [pages_by_number[number] for number in sorted(pages_by_number)]

    @staticmethod
    def _update_page_metrics(page: NormalizedPage | None, element: NormalizedElement) -> None:
        if page is None:
            return
        if element.type in {"heading", "paragraph", "list", "table"}:
            page.text_block_count += 1
            page.text_character_count += len(element.text or "")
        if element.type == "image":
            page.image_count += 1
            if element.bbox and page.width and page.height:
                page_area = page.width * page.height
                if page_area > 0:
                    page.largest_image_area_ratio = max(
                        page.largest_image_area_ratio, element.bbox.area / page_area
                    )

    @staticmethod
    def _apply_native_analysis(
        page_by_number: dict[int, NormalizedPage],
        native_pages: list[NativePdfPageAnalysis],
    ) -> None:
        for native in native_pages:
            page = page_by_number[native.page_number]
            page.width = native.width
            page.height = native.height
            page.native_text = native.native_text
            page.native_text_char_count = native.native_text_char_count
            page.native_text_block_count = native.native_text_block_count
            page.image_count = native.image_count
            page.largest_image_area_ratio = native.largest_image_area_ratio
            page.page_visual_type = native.page_visual_type
            page.needs_ocr = native.needs_ocr
            page.ocr_decision_reason = native.ocr_decision_reason
            page.visual_classification_status = (
                "pending" if native.page_visual_type == "ambiguous" else "classified"
            )
            page.ocr_status = "pending" if native.needs_ocr else "not_required"
            page.metadata = {
                **page.metadata,
                "native_word_count": native.native_word_count,
                "image_coverage_area_ratio": native.image_coverage_area_ratio,
            }

    @staticmethod
    def _link_reading_order(
        elements: list[NormalizedElement], relations: list[NormalizedRelation]
    ) -> None:
        for index, element in enumerate(elements):
            previous = elements[index - 1] if index > 0 else None
            following = elements[index + 1] if index + 1 < len(elements) else None
            if previous is not None:
                element.previous_id = previous.element_id
                relations.append(
                    NormalizedRelation(
                        source_id=element.element_id,
                        target_id=previous.element_id,
                        type="previous",
                    )
                )
            if following is not None:
                element.next_id = following.element_id
                relations.append(
                    NormalizedRelation(
                        source_id=element.element_id,
                        target_id=following.element_id,
                        type="next",
                    )
                )

    @staticmethod
    def _link_images_to_nearby_text(
        elements: list[NormalizedElement],
        assets: list[NormalizedAsset],
        relations: list[NormalizedRelation],
    ) -> None:
        asset_by_id = {asset.asset_id: asset for asset in assets}
        text_types = {"heading", "paragraph", "list", "table"}
        for index, element in enumerate(elements):
            if element.type != "image" or element.bbox is None:
                continue
            asset_id = element.metadata.get("asset_id")
            asset = asset_by_id.get(str(asset_id)) if asset_id else None
            if asset is None:
                continue
            nearby: list[NormalizedElement] = []
            for neighbor_index in (index - 1, index + 1):
                if neighbor_index < 0 or neighbor_index >= len(elements):
                    continue
                neighbor = elements[neighbor_index]
                if (
                    neighbor.type in text_types
                    and neighbor.page_number == element.page_number
                    and neighbor.bbox is not None
                ):
                    nearby.append(neighbor)
            for neighbor in nearby:
                if neighbor.element_id in asset.nearby_text_element_ids:
                    continue
                asset.nearby_text_element_ids.append(neighbor.element_id)
                relations.append(
                    NormalizedRelation(
                        source_id=neighbor.element_id,
                        target_id=asset.asset_id,
                        type="related_to",
                        metadata={"basis": "same_page_immediate_reading_order"},
                    )
                )
            asset.reading_order = element.reading_order
            previous = next((item for item in nearby if item.reading_order < element.reading_order), None)
            following = next((item for item in nearby if item.reading_order > element.reading_order), None)
            asset.previous_text_element_id = previous.element_id if previous else None
            asset.next_text_element_id = following.element_id if following else None

    @staticmethod
    def _save_picture(
        context: AdapterContext,
        document: Any,
        item: Any,
        order: int,
        page_number: int | None,
        bbox: BoundingBox | None,
    ) -> NormalizedAsset | None:
        image = item.get_image(document)
        if image is None:
            return None
        asset_id = str(
            uuid.uuid5(uuid.UUID(context.document_id), f"{context.version_id}:picture:{order}")
        )
        path = context.assets_directory / f"{asset_id}.png"
        image.save(path, format="PNG")
        return NormalizedAsset(
            asset_id=asset_id,
            type="image",
            source_page=page_number,
            bbox=bbox,
            local_path=context.storage_relative(path),
            visual_role="embedded_image",
            visual_classification_status="pending",
            metadata={"order": order, "width": image.width, "height": image.height},
        )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
