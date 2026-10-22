from corporate_rag_ingestion.adapters.docling_adapter import DoclingAdapter
from corporate_rag_ingestion.schemas import (
    BoundingBox,
    NormalizedAsset,
    NormalizedElement,
)


def test_image_is_linked_only_to_immediate_same_page_text() -> None:
    bbox = BoundingBox(left=0, top=0, right=10, bottom=10)
    before = NormalizedElement(
        element_id="paragraph-before", type="paragraph", text="Before", page_number=1,
        bbox=bbox, order=0, reading_order=0,
    )
    image = NormalizedElement(
        element_id="image-element", type="image", page_number=1, bbox=bbox, order=1,
        reading_order=1, metadata={"asset_id": "asset-1"},
    )
    after = NormalizedElement(
        element_id="paragraph-after", type="paragraph", text="After", page_number=1,
        bbox=bbox, order=2, reading_order=2,
    )
    unrelated = NormalizedElement(
        element_id="other-page", type="paragraph", text="Other", page_number=2,
        bbox=bbox, order=3, reading_order=3,
    )
    asset = NormalizedAsset(asset_id="asset-1", type="image", source_page=1, bbox=bbox,
                            local_path="assets/asset-1.png")
    relations = []

    DoclingAdapter._link_reading_order([before, image, after, unrelated], relations)
    DoclingAdapter._link_images_to_nearby_text(
        [before, image, after, unrelated], [asset], relations
    )

    assert asset.nearby_text_element_ids == ["paragraph-before", "paragraph-after"]
    assert asset.reading_order == 1
    assert asset.previous_text_element_id == "paragraph-before"
    assert asset.next_text_element_id == "paragraph-after"
    related = {(relation.source_id, relation.target_id) for relation in relations
               if relation.type == "related_to"}
    assert related == {
        ("paragraph-before", "asset-1"),
        ("paragraph-after", "asset-1"),
    }
    assert before.next_id == "image-element"
    assert image.previous_id == "paragraph-before"
    assert image.next_id == "paragraph-after"
