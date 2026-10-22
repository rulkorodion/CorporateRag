from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from ..schemas import (
    BoundingBox,
    NormalizedAsset,
    NormalizedDocument,
    NormalizedElement,
    NormalizedRelation,
)
from .models import OCRBlock, OCRLayoutElement, OCRPageResult
from .service import render_pdf_page


TEXT_TYPES = {"heading", "paragraph", "list", "table", "formula"}


def _stable_uuid(document_id: str, name: str) -> str:
    try:
        namespace = uuid.UUID(document_id)
    except ValueError:
        namespace = uuid.uuid5(uuid.NAMESPACE_URL, document_id)
    return str(uuid.uuid5(namespace, name))


def _normalized_type(label: str) -> str:
    return {
        "Title": "heading",
        "Section-header": "heading",
        "List-item": "list",
        "Table": "table",
        "Formula": "formula",
        "Picture": "image",
    }.get(label, "paragraph")


def _bbox(element: OCRLayoutElement) -> BoundingBox:
    x_min, y_min, x_max, y_max = element.original_pixel_bbox
    return BoundingBox(
        left=x_min,
        top=y_min,
        right=x_max,
        bottom=y_max,
        coordinate_origin="top-left-render-pixels",
    )


class OCRIntegrator:
    """Idempotently enrich one existing NormalizedDocument page with OCR."""

    def integrate(
        self,
        document: NormalizedDocument,
        result: OCRPageResult,
        *,
        source_path: Path,
        storage_root: Path,
        allow_ambiguous: bool = False,
    ) -> NormalizedDocument:
        self._validate_identity(document, result)
        if result.status != "COMPLETED" or result.page is None or result.provenance is None:
            raise ValueError("only COMPLETED OCR results can enrich NormalizedDocument")
        page = next((item for item in document.pages if item.page_number == result.page_number), None)
        if page is None:
            raise ValueError(f"normalized page not found: {result.page_number}")
        allowed_types = {"scan_candidate"}
        if allow_ambiguous:
            allowed_types.add("ambiguous")
        if page.page_visual_type not in allowed_types:
            raise ValueError(
                "OCR integration is not approved for page classification "
                f"{page.page_visual_type}"
            )
        fingerprint_digest = result.fingerprint.digest()
        existing_history = list(page.metadata.get("ocr_history") or [])
        if page.ocr_status == "completed" and any(
            item.get("fingerprint") == fingerprint_digest for item in existing_history
        ):
            return document

        managed_element_ids = {
            element.element_id
            for element in document.elements
            if element.page_number == result.page_number and element.metadata.get("ocr_managed")
        }
        managed_asset_ids = {
            asset.asset_id
            for asset in document.assets
            if asset.source_page == result.page_number and asset.metadata.get("ocr_managed")
        }
        document.elements = [
            element for element in document.elements if element.element_id not in managed_element_ids
        ]
        document.assets = [asset for asset in document.assets if asset.asset_id not in managed_asset_ids]
        removed_ids = managed_element_ids | managed_asset_ids
        document.relations = [
            relation
            for relation in document.relations
            if relation.source_id not in removed_ids and relation.target_id not in removed_ids
        ]

        layout = list(result.layout_elements)
        if not layout:
            layout = [self._layout_from_block(block, result) for block in result.blocks]
        layout.sort(key=lambda item: item.reading_order)
        table_by_order = {table.reading_order: table for table in result.tables}
        formula_by_order = {formula.reading_order: formula for formula in result.formulas}
        start_order = max((element.order for element in document.elements), default=-1) + 1
        new_elements: list[NormalizedElement] = []
        asset_by_element: dict[str, NormalizedAsset] = {}
        rendered = None

        for index, item in enumerate(layout):
            element_id = _stable_uuid(
                document.document_id,
                f"{document.version_id}:ocr:{result.page_number}:layout:{item.reading_order}",
            )
            element_type = _normalized_type(item.block_type)
            text = item.text or item.native_content or None
            metadata: dict[str, Any] = {
                "ocr_managed": True,
                "ocr_source": result.provenance.ocr_source,
                "ocr_block_type": item.block_type,
                "ocr_page_reading_order": item.reading_order,
                "raw_response": item.raw_response,
                "native_content": item.native_content,
                "model_confidence": None,
                "crop_sha256": item.crop_sha256,
                **item.metadata,
            }
            if item.block_type in {"Page-header", "Page-footer"}:
                metadata["exclude_from_rag_text"] = True
            table = table_by_order.get(item.reading_order)
            if table is not None:
                metadata.update(
                    {
                        "otsl": table.otsl,
                        "html": table.html,
                        "fallback_plain_text": table.fallback_plain_text,
                        "fallback_method": table.fallback_method,
                    }
                )
                text = table.html or table.fallback_plain_text or table.otsl or text
            formula = formula_by_order.get(item.reading_order)
            if formula is not None:
                metadata["latex"] = formula.latex
                text = formula.native_content or formula.latex
            if item.block_type == "Picture":
                text = None
                asset_id = _stable_uuid(
                    document.document_id,
                    f"{document.version_id}:ocr:{result.page_number}:picture:{item.reading_order}",
                )
                if rendered is None:
                    rendered = render_pdf_page(source_path, result.page_number, result.page.dpi)
                crop = self._crop_picture(rendered, item)
                asset_path = storage_root / "assets" / document.document_id / f"{asset_id}.png"
                asset_path.parent.mkdir(parents=True, exist_ok=True)
                crop.save(asset_path, format="PNG")
                asset = NormalizedAsset(
                    asset_id=asset_id,
                    type="image",
                    source_page=result.page_number,
                    bbox=_bbox(item),
                    local_path=asset_path.relative_to(storage_root).as_posix(),
                    visual_role="ocr_picture_region",
                    visual_classification_status="pending",
                    reading_order=item.reading_order,
                    metadata={
                        "ocr_managed": True,
                        "ocr_source": result.provenance.ocr_source,
                        "render_width": result.page.width,
                        "render_height": result.page.height,
                        "dpi": result.page.dpi,
                    },
                )
                asset_by_element[element_id] = asset
                document.assets.append(asset)
                metadata["asset_id"] = asset_id
            new_elements.append(
                NormalizedElement(
                    element_id=element_id,
                    type=element_type,  # type: ignore[arg-type]
                    text=text,
                    page_number=result.page_number,
                    bbox=_bbox(item),
                    order=start_order + index,
                    reading_order=item.reading_order,
                    metadata=metadata,
                )
            )

        self._link_ocr_reading_order(new_elements, asset_by_element, document.relations)
        document.elements.extend(new_elements)
        original_classification = page.metadata.get(
            "ocr_trigger_classification", page.page_visual_type
        )
        history = list(page.metadata.get("ocr_history") or [])
        provenance = result.provenance.model_dump(mode="json")
        history.append(
            {
                "status": "completed",
                "trigger_classification": original_classification,
                "selection_basis": (
                    "explicit_user_visual_review"
                    if original_classification == "ambiguous"
                    else "visual_decision_layer"
                ),
                "fingerprint": fingerprint_digest,
                "provenance": provenance,
            }
        )
        page.metadata = {
            **page.metadata,
            "ocr_trigger_classification": original_classification,
            "ocr_history": history,
            "ocr_text_for_rag": result.text.text_for_rag,
            "ocr_native_markdown": result.text.native_markdown,
            "ocr_blocks": [block.model_dump(mode="json") for block in result.blocks],
            "ocr_tables": [table.model_dump(mode="json") for table in result.tables],
            "ocr_formulas": [formula.model_dump(mode="json") for formula in result.formulas],
            "ocr_warnings": list(result.warnings),
        }
        page.ocr_status = "completed"
        page.ocr_provenance = provenance
        page.needs_ocr = False
        page.text_character_count = len(result.text.text_for_rag)
        page.text_block_count = len(result.blocks)
        document.text = "\n\n".join(
            str(item.metadata.get("ocr_text_for_rag") or item.native_text).strip()
            for item in sorted(document.pages, key=lambda value: value.page_number)
            if str(item.metadata.get("ocr_text_for_rag") or item.native_text).strip()
        )
        completed_pages = sorted(
            item.page_number for item in document.pages if item.ocr_status == "completed"
        )
        document.metadata = {
            **document.metadata,
            "ocr_completed_pages": completed_pages,
            "ocr_integration_version": "1.0",
        }
        return document

    @staticmethod
    def _validate_identity(document: NormalizedDocument, result: OCRPageResult) -> None:
        expected = {
            "document_id": document.document_id,
            "version_id": document.version_id,
            "relative_path": str(document.metadata.get("relative_path") or "").replace("\\", "/"),
            "source_sha256": str(document.metadata.get("sha256") or "").casefold(),
        }
        actual = {
            "document_id": result.document_id,
            "version_id": result.version_id,
            "relative_path": result.relative_path,
            "source_sha256": result.source_sha256,
        }
        if actual != expected:
            raise ValueError(f"OCR/NormalizedDocument identity mismatch: {actual!r}")

    @staticmethod
    def _layout_from_block(block: OCRBlock, result: OCRPageResult) -> OCRLayoutElement:
        if result.page is None:
            raise ValueError("OCR block fallback requires page geometry")
        bbox = block.bounding_box
        return OCRLayoutElement(
            block_type=block.block_type,
            page_number=block.page_number,
            reading_order=int(block.reading_order or 0),
            bbox_source_width=result.page.width,
            bbox_source_height=result.page.height,
            original_page_width=result.page.width,
            original_page_height=result.page.height,
            normalized_bbox=[value * 1000 for value in bbox.relative()],
            original_pixel_bbox=[
                round(bbox.x_min), round(bbox.y_min), round(bbox.x_max), round(bbox.y_max)
            ],
            text=block.text,
            native_content=block.text,
            model_confidence=None,
            metadata=block.metadata,
        )

    @staticmethod
    def _crop_picture(rendered: Any, element: OCRLayoutElement):  # type: ignore[no-untyped-def]
        x_min, y_min, x_max, y_max = element.original_pixel_bbox
        scale_x = rendered.width / element.original_page_width
        scale_y = rendered.height / element.original_page_height
        return rendered.crop(
            (
                max(0, round(x_min * scale_x)),
                max(0, round(y_min * scale_y)),
                min(rendered.width, round(x_max * scale_x)),
                min(rendered.height, round(y_max * scale_y)),
            )
        )

    @staticmethod
    def _link_ocr_reading_order(
        elements: list[NormalizedElement],
        asset_by_element: dict[str, NormalizedAsset],
        relations: list[NormalizedRelation],
    ) -> None:
        for index, element in enumerate(elements):
            previous = elements[index - 1] if index else None
            following = elements[index + 1] if index + 1 < len(elements) else None
            if previous is not None:
                element.previous_id = previous.element_id
                relations.append(
                    NormalizedRelation(
                        source_id=element.element_id,
                        target_id=previous.element_id,
                        type="previous",
                        metadata={"basis": "ocr_layout_reading_order"},
                    )
                )
            if following is not None:
                element.next_id = following.element_id
                relations.append(
                    NormalizedRelation(
                        source_id=element.element_id,
                        target_id=following.element_id,
                        type="next",
                        metadata={"basis": "ocr_layout_reading_order"},
                    )
                )
            asset = asset_by_element.get(element.element_id)
            if asset is None:
                continue
            previous_text = next(
                (candidate for candidate in reversed(elements[:index]) if candidate.type in TEXT_TYPES),
                None,
            )
            next_text = next(
                (candidate for candidate in elements[index + 1 :] if candidate.type in TEXT_TYPES),
                None,
            )
            asset.previous_text_element_id = previous_text.element_id if previous_text else None
            asset.next_text_element_id = next_text.element_id if next_text else None
            asset.nearby_text_element_ids = [
                candidate.element_id for candidate in (previous_text, next_text) if candidate is not None
            ]
            for candidate in (previous_text, next_text):
                if candidate is not None:
                    relations.append(
                        NormalizedRelation(
                            source_id=candidate.element_id,
                            target_id=asset.asset_id,
                            type="related_to",
                            metadata={"basis": "ocr_layout_immediate_neighbor"},
                        )
                    )
