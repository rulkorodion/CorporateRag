from __future__ import annotations

from pathlib import Path

from corporate_rag_ingestion.context.models import RelatedImage
from corporate_rag_ingestion.generation.models import GenerationLatency, RAGAnswer, UsedSource
from corporate_rag_ingestion.schemas import (
    BoundingBox,
    NormalizedAsset,
    NormalizedDocument,
    NormalizedElement,
    NormalizedRelation,
)
from corporate_rag_ingestion.ui.files import (
    asset_file,
    mime_type,
    safe_path,
    safe_asset_path,
    safe_source_path,
    source_file,
    source_location,
)
from corporate_rag_ingestion.ui.presentation import (
    NormalizedPresentationRepository,
    answer_blocks,
    pdf_clip_rect,
)


def source(relative_path: str) -> UsedSource:
    return UsedSource(
        citation="[1]",
        source_number=1,
        chunk_id="chunk-1",
        document_id="doc-1",
        version_id="version-1",
        relative_path=relative_path,
        page_start=2,
        page_end=4,
        heading_path=["Раздел"],
        section_title="Подраздел",
        source_type="native",
        related_image_ids=["image-1"],
    )


def test_source_and_asset_files_resolve_only_inside_roots(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    storage = tmp_path / "storage"
    document = knowledge / "folder" / "manual.pdf"
    image = storage / "assets" / "doc-1" / "image.png"
    document.parent.mkdir(parents=True)
    image.parent.mkdir(parents=True)
    document.write_bytes(b"pdf")
    image.write_bytes(b"png")

    assert source_file(knowledge, source("folder/manual.pdf")) == document.resolve()
    assert asset_file(storage, "assets/doc-1/image.png") == image.resolve()
    assert safe_path(knowledge, "../outside.txt") is None
    assert safe_path(knowledge, "folder\\manual.pdf") == document.resolve()
    assert source_file(knowledge, source("missing.pdf")) is None
    secret = knowledge / "folder" / "secret.env"
    secret.write_text("secret", encoding="utf-8")
    assert safe_source_path(knowledge, "folder/secret.env") is None
    assert safe_path(knowledge, "folder/\x00invalid.pdf") is None
    outside_asset = storage / "private" / "visual.png"
    outside_asset.parent.mkdir(parents=True)
    outside_asset.write_bytes(b"png")
    assert (
        safe_asset_path(
            knowledge_base_root=knowledge,
            storage_root=storage,
            relative_path="private/visual.png",
            scope="storage",
        )
        is None
    )


def test_source_label_and_mime_type() -> None:
    item = source("folder/manual.pdf")

    assert source_location(item) == "folder/manual.pdf, стр. 2–4 · Подраздел"
    assert mime_type(Path("manual.pdf")) == "application/pdf"
    assert mime_type(Path("unknown.custom-extension")) == "application/octet-stream"


def write_normalized(root: Path, document: NormalizedDocument) -> None:
    path = root / document.document_id / f"{document.version_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document.model_dump_json(), encoding="utf-8")


def rag_answer(source_item: UsedSource, *, images: list[RelatedImage] | None = None) -> RAGAnswer:
    return RAGAnswer(
        query="Как настроить?",
        answer="Первое действие [1].\n\nВторое действие [1].",
        status="completed",
        citation_validation="valid",
        used_sources=[source_item],
        related_images=images or [],
        context_id="context-1",
        model="fake",
        generation_version="test",
        generation_settings_hash="a" * 64,
        latency=GenerationLatency(),
    )


def test_standalone_visual_is_placed_inline_without_text_relations(tmp_path: Path) -> None:
    root = tmp_path / "normalized"
    item = source("Отдел ИТ/Biosmart.png").model_copy(
        update={
            "source_element_ids": ["visual-element"],
            "related_image_ids": ["visual-asset"],
            "provenance": {"source_fallback": "standalone_visual_filename"},
        }
    )
    write_normalized(
        root,
        NormalizedDocument(
            document_id="doc-1",
            version_id="version-1",
            metadata={"relative_path": "Отдел ИТ/Biosmart.png"},
            elements=[
                NormalizedElement(element_id="visual-element", type="image", order=0)
            ],
            assets=[
                NormalizedAsset(
                    asset_id="visual-asset",
                    type="image",
                    local_path="Отдел ИТ/Biosmart.png",
                    metadata={
                        "width": 1000,
                        "height": 700,
                        "path_scope": "knowledge_base",
                    },
                )
            ],
        ),
    )
    answer = rag_answer(
        item,
        images=[
            RelatedImage(
                asset_id="visual-asset",
                document_id="doc-1",
                version_id="version-1",
                local_path="Отдел ИТ/Biosmart.png",
                visual_role="standalone_visual",
                visual_classification_status="pending",
                path_scope="knowledge_base",
            )
        ],
    )

    placements = NormalizedPresentationRepository(root).inline_images(
        answer, answer_blocks(answer.answer)
    )

    assert placements[0][0].asset_id == "visual-asset"
    assert placements[0][0].path_scope == "knowledge_base"


def test_standalone_visual_suppresses_images_from_supplementary_text_sources(
    tmp_path: Path,
) -> None:
    root = tmp_path / "normalized"
    visual = source("visual.png").model_copy(
        update={
            "source_element_ids": ["visual-element"],
            "related_image_ids": ["visual-asset"],
            "provenance": {"source_fallback": "standalone_visual_filename"},
        }
    )
    supplement = source("manual.pdf").model_copy(
        update={
            "source_number": 2,
            "citation": "[2]",
            "document_id": "doc-2",
            "version_id": "version-2",
            "source_element_ids": ["text-element"],
            "related_image_ids": ["extra-image"],
        }
    )
    for document in (
        NormalizedDocument(
            document_id="doc-1",
            version_id="version-1",
            metadata={"relative_path": "visual.png"},
            elements=[NormalizedElement(element_id="visual-element", type="image", order=0)],
            assets=[
                NormalizedAsset(
                    asset_id="visual-asset",
                    type="image",
                    local_path="visual.png",
                    metadata={"width": 800, "height": 600, "path_scope": "knowledge_base"},
                )
            ],
        ),
        NormalizedDocument(
            document_id="doc-2",
            version_id="version-2",
            metadata={"relative_path": "manual.pdf"},
            elements=[
                NormalizedElement(
                    element_id="text-element", type="paragraph", text="Дополнение", order=0
                )
            ],
            assets=[
                NormalizedAsset(
                    asset_id="extra-image",
                    type="image",
                    local_path="assets/extra.png",
                    nearby_text_element_ids=["text-element"],
                    metadata={"width": 800, "height": 600},
                )
            ],
        ),
    ):
        write_normalized(root, document)
    answer = rag_answer(
        visual,
        images=[
            RelatedImage(
                asset_id="visual-asset",
                document_id="doc-1",
                version_id="version-1",
                local_path="visual.png",
                visual_role="standalone_visual",
                visual_classification_status="pending",
                path_scope="knowledge_base",
            ),
            RelatedImage(
                asset_id="extra-image",
                document_id="doc-2",
                version_id="version-2",
                local_path="assets/extra.png",
                visual_role="embedded_image",
                visual_classification_status="pending",
            ),
        ],
    ).model_copy(
        update={
            "answer": "Найден визуальный файл [1]. Дополнительный факт [2].",
            "used_sources": [visual, supplement],
        }
    )

    placements = NormalizedPresentationRepository(root).inline_images(
        answer, answer_blocks(answer.answer)
    )

    assert [image.asset_id for image in placements[0]] == ["visual-asset"]


def test_transcript_source_attachment_resolves_to_linked_video(tmp_path: Path) -> None:
    root = tmp_path / "normalized"
    transcript = NormalizedDocument(
        document_id="transcript",
        version_id="transcript-v1",
        metadata={"relative_path": "Видео/Текстовый Урок.docx"},
        relations=[
            NormalizedRelation(
                source_id="transcript",
                target_id="video",
                type="transcript_of",
            )
        ],
    )
    video = NormalizedDocument(
        document_id="video",
        version_id="video-v1",
        metadata={"relative_path": "Видео/Урок.mp4"},
    )
    write_normalized(root, transcript)
    write_normalized(root, video)
    item = source("Видео/Текстовый Урок.docx").model_copy(
        update={"document_id": "transcript", "version_id": "transcript-v1"}
    )

    attachment = NormalizedPresentationRepository(root).source_attachment(item)

    assert attachment.media_type == "video"
    assert attachment.display_relative_path == "Видео/Урок.mp4"
    assert attachment.indexed_relative_path == "Видео/Текстовый Урок.docx"
    assert attachment.document_id == "video"
    assert attachment.version_id == "video-v1"


def test_embedded_images_are_not_presented_even_when_structurally_linked(tmp_path: Path) -> None:
    root = tmp_path / "normalized"
    item = source("manual.pdf").model_copy(
        update={
            "source_element_ids": ["step-1", "step-2"],
            "related_image_ids": ["screen-1", "logo", "unrelated"],
        }
    )
    document = NormalizedDocument(
        document_id="doc-1",
        version_id="version-1",
        metadata={"relative_path": "manual.pdf"},
        elements=[
            NormalizedElement(
                element_id="step-1", type="paragraph", text="Первое действие", order=1
            ),
            NormalizedElement(
                element_id="step-2", type="paragraph", text="Второе действие", order=2
            ),
        ],
        assets=[
            NormalizedAsset(
                asset_id="screen-1",
                type="image",
                source_page=2,
                local_path="assets/doc-1/screen.png",
                nearby_text_element_ids=["step-2"],
                metadata={"width": 1000, "height": 700, "order": 2},
            ),
            NormalizedAsset(
                asset_id="logo",
                type="image",
                source_page=2,
                local_path="assets/doc-1/logo.png",
                nearby_text_element_ids=["step-1"],
                metadata={"width": 150, "height": 40, "order": 1},
            ),
            NormalizedAsset(
                asset_id="unrelated",
                type="image",
                source_page=2,
                local_path="assets/doc-1/other.png",
                nearby_text_element_ids=["outside-chunk"],
                metadata={"width": 1000, "height": 700, "order": 3},
            ),
        ],
        relations=[
            NormalizedRelation(source_id="step-2", target_id="screen-1", type="related_to"),
            NormalizedRelation(source_id="step-1", target_id="logo", type="related_to"),
        ],
    )
    write_normalized(root, document)
    images = [
        RelatedImage(
            asset_id=asset_id,
            document_id="doc-1",
            version_id="version-1",
            local_path=f"assets/doc-1/{asset_id}.png",
            source_page=2,
            visual_role="embedded_image",
            visual_classification_status="pending",
        )
        for asset_id in item.related_image_ids
    ]
    answer = rag_answer(item, images=images)
    blocks = answer_blocks(answer.answer)

    placements = NormalizedPresentationRepository(root).inline_images(answer, blocks)

    assert placements == {}


def test_answer_block_split_preserves_paragraph_groups() -> None:
    assert answer_blocks("Абзац [1].\n\n- пункт один\n- пункт два [2].") == [
        "Абзац [1].",
        "- пункт один\n- пункт два [2].",
    ]


def test_pdf_bottom_left_bbox_converts_for_high_resolution_render() -> None:
    assert pdf_clip_rect(
        BoundingBox(
            left=10,
            top=700,
            right=210,
            bottom=500,
            coordinate_origin="BOTTOMLEFT",
        ),
        page_height=800,
    ) == (10, 100, 210, 300)
