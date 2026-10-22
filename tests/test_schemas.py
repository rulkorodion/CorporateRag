from __future__ import annotations

import json
from pathlib import Path

from corporate_rag_ingestion.schemas import (
    BoundingBox,
    NormalizedDocument,
    NormalizedAsset,
    NormalizedElement,
    NormalizedRelation,
)
from corporate_rag_ingestion.storage import NormalizedStorage


def test_normalized_document_serialization_round_trip(tmp_path: Path) -> None:
    document = NormalizedDocument(
        document_id="doc-1",
        version_id="version-1",
        metadata={"title": "Регламент"},
        text="Text",
        elements=[
            NormalizedElement(
                element_id="element-1",
                type="paragraph",
                text="Text",
                page_number=1,
                bbox=BoundingBox(left=1, top=2, right=3, bottom=4),
                order=0,
                reading_order=0,
            )
        ],
        assets=[
            NormalizedAsset(
                asset_id="asset-1",
                type="image",
                source_page=1,
                local_path="assets/asset-1.png",
                nearby_text_element_ids=["element-1"],
            )
        ],
        relations=[
            NormalizedRelation(
                source_id="element-1", target_id="element-2", type="related_to"
            )
        ],
    )
    storage = NormalizedStorage(tmp_path / "storage")

    path = storage.save(document)
    restored = NormalizedDocument.model_validate(json.loads(path.read_text(encoding="utf-8")))

    assert restored == document
    assert path.name == "version-1.json"
