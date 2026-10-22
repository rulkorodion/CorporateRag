from __future__ import annotations

from ..schemas import NormalizedDocument, NormalizedElement
from .models import ChunkedDocument
from .settings import ChunkingSettings
from .tokens import TokenEstimator


def validate_chunked_document(
    source: NormalizedDocument,
    chunked: ChunkedDocument,
    settings: ChunkingSettings,
) -> None:
    estimator = TokenEstimator()
    element_by_id = {element.element_id: element for element in source.elements}
    for derived in chunked.metadata.get("derived_native_page_sources", []):
        element_id = str(derived["element_id"])
        page_number = int(derived["page_number"])
        page = next((item for item in source.pages if item.page_number == page_number), None)
        if page is None or not (page.native_text or "").strip():
            raise ValueError(f"invalid derived native page source: {element_id}")
        element_by_id[element_id] = NormalizedElement(
            element_id=element_id,
            type="paragraph",
            text=page.native_text,
            page_number=page_number,
            order=0,
            metadata={"derived_from": "normalized_page.native_text"},
        )
    asset_ids = {asset.asset_id for asset in source.assets}
    for chunk in chunked.chunks:
        if not chunk.text.strip():
            raise ValueError(f"empty retrieval chunk: {chunk.chunk_id}")
        actual_tokens = estimator.count(chunk.text)
        if chunk.token_count != actual_tokens:
            raise ValueError(f"token count mismatch: {chunk.chunk_id}")
        if actual_tokens > settings.hard_max_tokens and not chunk.metadata.get(
            "hard_max_exception"
        ):
            raise ValueError(
                f"chunk exceeds hard max without explicit reason: {chunk.chunk_id} ({actual_tokens})"
            )
        missing_elements = [
            element_id for element_id in chunk.source_element_ids if element_id not in element_by_id
        ]
        if missing_elements:
            raise ValueError(f"chunk references missing source elements: {missing_elements}")
        if chunk.metadata.get("source_fallback") == "standalone_visual_filename":
            if not all(element_by_id[element_id].type == "image" for element_id in chunk.source_element_ids):
                raise ValueError("standalone visual filename chunk must reference image elements")
        missing_assets = [asset_id for asset_id in chunk.related_image_ids if asset_id not in asset_ids]
        if missing_assets:
            raise ValueError(f"chunk references missing image assets: {missing_assets}")
        invalid_tables = [
            element_id
            for element_id in chunk.related_table_ids
            if element_id not in element_by_id or element_by_id[element_id].type != "table"
        ]
        if invalid_tables:
            raise ValueError(f"chunk references invalid table elements: {invalid_tables}")
        pages = sorted(
            {
                element_by_id[element_id].page_number
                for element_id in chunk.source_element_ids
                if element_by_id[element_id].page_number is not None
            }
        )
        if pages:
            if chunk.page_start != pages[0] or chunk.page_end != pages[-1]:
                raise ValueError(f"chunk page range does not match source elements: {chunk.chunk_id}")
        elif chunk.page_start is not None or chunk.page_end is not None:
            raise ValueError(f"pageless source has non-null chunk page range: {chunk.chunk_id}")
    if chunked.coverage.missing_source_element_ids:
        raise ValueError(
            f"content coverage gap: {chunked.coverage.missing_source_element_ids[:10]}"
        )
    if chunked.coverage.eligible_source_elements != chunked.coverage.covered_source_elements:
        raise ValueError("eligible and covered source element counts differ")
