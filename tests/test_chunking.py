from __future__ import annotations

from pathlib import Path

from corporate_rag_ingestion.chunking.chunker import StructuralChunker
from corporate_rag_ingestion.chunking.models import ChunkedDocument
from corporate_rag_ingestion.chunking.overrides import ChunkingOverrides
from corporate_rag_ingestion.chunking.settings import ChunkingSettings
from corporate_rag_ingestion.chunking.storage import ChunkStorage
from corporate_rag_ingestion.schemas import (
    NormalizedAsset,
    NormalizedDocument,
    NormalizedElement,
    NormalizedPage,
    NormalizedRelation,
)


def document_with(elements: list[NormalizedElement], **kwargs: object) -> NormalizedDocument:
    return NormalizedDocument(
        document_id="11111111-1111-5111-8111-111111111111",
        version_id="version-1",
        metadata={
            "relative_path": "folder/document.pdf",
            "extension": ".pdf",
            "sha256": "a" * 64,
        },
        elements=elements,
        **kwargs,
    )


def test_small_paragraphs_merge_across_pages_and_keep_heading_and_image() -> None:
    elements = [
        NormalizedElement(
            element_id="h1", type="heading", text="1. Общие положения", page_number=1, order=0
        ),
        NormalizedElement(
            element_id="p1", type="paragraph", text="Это начало предложения", page_number=1, order=1
        ),
        NormalizedElement(
            element_id="p2",
            type="paragraph",
            text="которое продолжается на следующей странице.",
            page_number=2,
            order=2,
        ),
        NormalizedElement(
            element_id="h2", type="heading", text="2. Ответственность", page_number=2, order=3
        ),
        NormalizedElement(
            element_id="p3", type="paragraph", text="Ответственный назначается приказом.", page_number=2, order=4
        ),
    ]
    asset = NormalizedAsset(
        asset_id="image-1",
        type="image",
        source_page=2,
        local_path="assets/image-1.png",
        nearby_text_element_ids=["p2"],
    )
    document = document_with(
        elements,
        pages=[NormalizedPage(page_number=1), NormalizedPage(page_number=2)],
        assets=[asset],
        relations=[NormalizedRelation(source_id="p2", target_id="image-1", type="related_to")],
    )
    result = StructuralChunker().chunk(document)

    assert len(result.chunks) == 2
    first, second = result.chunks
    assert first.page_start == 1 and first.page_end == 2
    assert first.heading_path == ["1. Общие положения"]
    assert "предложения которое продолжается" in first.text
    assert first.related_image_ids == ["image-1"]
    assert first.next_chunk_id == second.chunk_id
    assert second.previous_chunk_id == first.chunk_id
    assert result.coverage.missing_source_element_ids == []


def test_standalone_visual_filename_becomes_retrievable_and_returns_asset() -> None:
    document = NormalizedDocument(
        document_id="11111111-1111-5111-8111-111111111111",
        version_id="version-visual",
        metadata={
            "relative_path": "Отдел ИТ/Сканирование ладони на сканере Biosmart.png",
            "extension": ".png",
            "sha256": "b" * 64,
        },
        elements=[
            NormalizedElement(
                element_id="visual-element",
                type="image",
                order=0,
                metadata={
                    "asset_id": "visual-asset",
                    "searchable_title": "Сканирование ладони на сканере Biosmart",
                },
            )
        ],
        assets=[
            NormalizedAsset(
                asset_id="visual-asset",
                type="image",
                local_path="Отдел ИТ/Сканирование ладони на сканере Biosmart.png",
                visual_role="standalone_visual",
                metadata={"path_scope": "knowledge_base"},
            )
        ],
    )

    result = StructuralChunker().chunk(document)

    assert len(result.chunks) == 1
    assert "Сканирование ладони" in result.chunks[0].text
    assert result.chunks[0].related_image_ids == ["visual-asset"]
    assert result.chunks[0].metadata["source_fallback"] == "standalone_visual_filename"


def test_large_section_splits_without_exceeding_hard_limit_and_is_deterministic() -> None:
    settings = ChunkingSettings(
        target_tokens=55,
        soft_max_tokens=65,
        hard_max_tokens=80,
        minimum_chunk_tokens=10,
    )
    long_text = " ".join(f"предложение{i}." for i in range(250))
    document = document_with(
        [
            NormalizedElement(
                element_id="heading", type="heading", text="5. Маркировка", page_number=1, order=0
            ),
            NormalizedElement(
                element_id="long", type="paragraph", text=long_text, page_number=1, order=1
            ),
        ],
        pages=[NormalizedPage(page_number=1)],
    )
    chunker = StructuralChunker(settings)
    first = chunker.chunk(document)
    second = chunker.chunk(document)

    assert len(first.chunks) > 2
    assert max(chunk.token_count for chunk in first.chunks) <= settings.hard_max_tokens
    assert all(chunk.heading_path == ["5. Маркировка"] for chunk in first.chunks)
    assert all("long" in chunk.source_element_ids for chunk in first.chunks)
    assert [chunk.chunk_id for chunk in first.chunks] == [
        chunk.chunk_id for chunk in second.chunks
    ]
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_small_html_table_stays_whole() -> None:
    table = NormalizedElement(
        element_id="table-1",
        type="table",
        text="<table><tr><th>Марка</th><th>Цена</th></tr><tr><td>А</td><td>100</td></tr></table>",
        page_number=3,
        order=1,
        metadata={
            "html": "<table><tr><th>Марка</th><th>Цена</th></tr><tr><td>А</td><td>100</td></tr></table>"
        },
    )
    document = document_with(
        [
            NormalizedElement(
                element_id="heading", type="heading", text="3. Цены", page_number=3, order=0
            ),
            table,
        ],
        pages=[NormalizedPage(page_number=3)],
    )
    result = StructuralChunker().chunk(document)

    assert len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.chunk_type == "table"
    assert chunk.related_table_ids == ["table-1"]
    assert chunk.heading_path == ["3. Цены"]
    assert "Марка=А" in chunk.text and "Цена=100" in chunk.text
    assert "<th>Марка</th>" in chunk.metadata["table_html"]


def test_large_excel_table_splits_rows_and_repeats_headers() -> None:
    cells = [
        {"coordinate": "A1", "row": 1, "column": 1, "value": "Код", "data_type": "s"},
        {"coordinate": "B1", "row": 1, "column": 2, "value": "Описание", "data_type": "s"},
    ]
    for row in range(2, 62):
        cells.extend(
            [
                {"coordinate": f"A{row}", "row": row, "column": 1, "value": row, "data_type": "n"},
                {
                    "coordinate": f"B{row}",
                    "row": row,
                    "column": 2,
                    "value": f"Подробное описание строки {row}",
                    "data_type": "s",
                },
            ]
        )
    table = NormalizedElement(
        element_id="sheet-1",
        type="table",
        order=0,
        metadata={"sheet_name": "Товары", "used_range": "A1:B61", "cells": cells},
    )
    settings = ChunkingSettings(
        target_tokens=80,
        soft_max_tokens=100,
        hard_max_tokens=120,
        minimum_chunk_tokens=10,
    )
    result = StructuralChunker(settings).chunk(document_with([table]))

    assert len(result.chunks) > 2
    assert all(chunk.chunk_type == "table" for chunk in result.chunks)
    assert all("Headers: Код | Описание" in chunk.text for chunk in result.chunks)
    assert all(chunk.token_count <= settings.hard_max_tokens for chunk in result.chunks)
    assert result.chunks[0].metadata["row_start"] == "2"
    assert result.chunks[-1].metadata["row_end"] == "61"


def test_completed_ocr_replaces_native_garbage_and_preserves_provenance() -> None:
    provenance = {
        "ocr_source": "imported",
        "engine_name": "MonkeyOCR",
        "ocr_model_id": "model",
        "ocr_model_revision": "revision",
        "ocr_settings_hash": "b" * 64,
        "processed_at": "2026-08-10T00:00:00Z",
        "runtime": {},
        "processing_metadata": {},
    }
    document = document_with(
        [
            NormalizedElement(
                element_id="native-garbage",
                type="paragraph",
                text="\x01\x02broken native",
                page_number=1,
                order=0,
            ),
            NormalizedElement(
                element_id="ocr-heading",
                type="heading",
                text="Распознанный раздел",
                page_number=1,
                order=1,
                metadata={"ocr_managed": True, "ocr_block_type": "Section-header"},
            ),
            NormalizedElement(
                element_id="ocr-text",
                type="paragraph",
                text="Корректный распознанный текст.",
                page_number=1,
                order=2,
                metadata={"ocr_managed": True, "ocr_block_type": "Text"},
            ),
        ],
        pages=[
            NormalizedPage(
                page_number=1,
                page_visual_type="scan_candidate",
                ocr_status="completed",
                ocr_provenance=provenance,
                metadata={"ocr_text_for_rag": "Корректный распознанный текст."},
            )
        ],
    )
    result = StructuralChunker().chunk(document)

    assert len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.source_type == "ocr"
    assert "Корректный" in chunk.text
    assert "broken native" not in chunk.text
    assert "native-garbage" not in chunk.source_element_ids
    assert chunk.metadata["ocr_provenance"][0]["ocr_source"] == "imported"
    assert result.coverage.skipped_source_elements["native_superseded_by_completed_ocr"] == 1


def test_chunk_storage_second_save_is_byte_identical(tmp_path: Path) -> None:
    document = document_with(
        [
            NormalizedElement(
                element_id="p1", type="paragraph", text="Стабильный текст.", page_number=1, order=0
            )
        ],
        pages=[NormalizedPage(page_number=1)],
    )
    chunked = StructuralChunker().chunk(document)
    storage = ChunkStorage(tmp_path / "chunks")
    path, first_changed = storage.save(chunked)
    first_bytes = path.read_bytes()
    _, second_changed = storage.save(chunked)

    assert first_changed is True
    assert second_changed is False
    assert path.read_bytes() == first_bytes
    assert ChunkedDocument.model_validate_json(path.read_text(encoding="utf-8")) == chunked


def test_explicit_page_override_is_traceable_and_changes_settings_hash() -> None:
    document = document_with(
        [
            NormalizedElement(
                element_id="rejected", type="paragraph", text="Rejected visual page", page_number=1, order=0
            ),
            NormalizedElement(
                element_id="kept", type="paragraph", text="Searchable content", page_number=2, order=1
            ),
        ],
        pages=[NormalizedPage(page_number=1), NormalizedPage(page_number=2)],
    )
    overrides = ChunkingOverrides(
        excluded_pages={"folder/document.pdf": {1}},
        notes={"basis": "explicit_human_review"},
    )
    result = StructuralChunker(overrides=overrides).chunk(document)
    baseline = StructuralChunker().chunk(document)

    assert [chunk.source_element_ids for chunk in result.chunks] == [["kept"]]
    assert result.metadata["explicitly_excluded_pages"] == [1]
    assert result.coverage.skipped_source_elements["explicitly_excluded_page"] == 1
    assert result.settings_hash != baseline.settings_hash


def test_single_oversized_table_row_splits_by_cell_below_hard_limit() -> None:
    settings = ChunkingSettings(
        target_tokens=80,
        soft_max_tokens=100,
        hard_max_tokens=140,
        minimum_chunk_tokens=10,
    )
    long_a = " ".join(f"first-{index}" for index in range(90))
    long_b = " ".join(f"second-{index}" for index in range(90))
    table = NormalizedElement(
        element_id="verbose-table",
        type="table",
        text=f"| Scenario A | Scenario B |\n| --- | --- |\n| {long_a} | {long_b} |",
        page_number=3,
        order=0,
    )
    result = StructuralChunker(settings).chunk(
        document_with([table], pages=[NormalizedPage(page_number=3)])
    )

    assert len(result.chunks) > 1
    assert all(chunk.chunk_type == "table" for chunk in result.chunks)
    assert all(chunk.token_count <= settings.hard_max_tokens for chunk in result.chunks)
    assert all("Headers: Scenario A | Scenario B" in chunk.text for chunk in result.chunks)
    assert all(chunk.related_table_ids == ["verbose-table"] for chunk in result.chunks)


def test_missing_structural_page_uses_normalized_native_text_without_mutation() -> None:
    page = NormalizedPage(
        page_number=8,
        native_text="Native page text that Docling did not emit as an element.",
        native_text_char_count=57,
        page_visual_type="text",
        ocr_status="not_required",
    )
    document = document_with([], pages=[page])
    original = document.model_dump(mode="json")

    result = StructuralChunker().chunk(document)

    assert len(result.chunks) == 1
    assert result.chunks[0].page_start == 8
    assert result.chunks[0].source_type == "native"
    assert "Docling did not emit" in result.chunks[0].text
    assert len(result.metadata["derived_native_page_sources"]) == 1
    assert result.chunks[0].metadata["source_fallback"] == "native_page_text"
    assert result.chunks[0].metadata["source_fallback_reason"] == (
        "docling_structural_elements_missing_for_page"
    )
    assert result.chunks[0].metadata["fallback_page_numbers"] == [8]
    assert result.chunks[0].metadata["fallback_source_ids"] == (
        result.chunks[0].source_element_ids
    )
    assert document.model_dump(mode="json") == original


def test_weak_title_is_coalesced_but_clear_noise_is_only_marked() -> None:
    document = document_with(
        [
            NormalizedElement(
                element_id="title", type="paragraph", text="Базовый курс", page_number=1, order=0
            ),
            NormalizedElement(
                element_id="heading", type="heading", text="1. Доставка", page_number=1, order=1
            ),
            NormalizedElement(
                element_id="body", type="paragraph", text="Порядок оформления доставки.", page_number=1, order=2
            ),
        ],
        pages=[NormalizedPage(page_number=1)],
    )
    result = StructuralChunker().chunk(document)

    assert len(result.chunks) == 1
    assert result.chunks[0].source_element_ids == ["title", "heading", "body"]
    assert result.chunks[0].metadata["coalesced_small_text"] is True

    noise = StructuralChunker().chunk(
        document_with(
            [NormalizedElement(element_id="noise", type="paragraph", text="---", page_number=1, order=0)],
            pages=[NormalizedPage(page_number=1)],
        )
    )
    assert noise.chunks[0].metadata["retrieval_candidate"] is False
    assert noise.chunks[0].metadata["retrieval_exclusion_reason"] == "low_semantic_signal"
