from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from corporate_rag_ingestion.chunking.models import RetrievalChunk
from corporate_rag_ingestion.context.builder import ContextBuilder
from corporate_rag_ingestion.context.models import RelatedImage
from corporate_rag_ingestion.context.repository import AssetRepository
from corporate_rag_ingestion.context.settings import ContextBuilderSettings
from corporate_rag_ingestion.retrieval.models import SearchResult
from corporate_rag_ingestion.schemas import NormalizedAsset, NormalizedDocument


class FakeChunkRepository:
    def __init__(self, chunks: list[RetrievalChunk]) -> None:
        self.chunks = {chunk.chunk_id: chunk for chunk in chunks}

    def get(self, chunk_id: str) -> RetrievalChunk | None:
        return self.chunks.get(chunk_id)


class FakeAssetRepository:
    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = existing or set()

    def resolve(self, document_id: str, version_id: str, asset_ids: list[str]):  # type: ignore[no-untyped-def]
        found = [
            RelatedImage(
                asset_id=asset_id,
                document_id=document_id,
                version_id=version_id,
                local_path=f"assets/{document_id}/{asset_id}.png",
                source_page=1,
                visual_role="embedded_image",
                visual_classification_status="pending",
            )
            for asset_id in asset_ids
            if asset_id in self.existing
        ]
        missing = [asset_id for asset_id in asset_ids if asset_id not in self.existing]
        return found, missing


def make_chunk(
    chunk_id: str,
    text: str,
    *,
    document_id: str = "doc-1",
    chunk_type: str = "text",
    previous: str | None = None,
    next_: str | None = None,
    parent: str | None = "section-1",
    metadata: dict | None = None,
    images: list[str] | None = None,
    source_type: str = "native",
) -> RetrievalChunk:
    return RetrievalChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        version_id=f"version-{document_id}",
        relative_path=f"folder/{document_id}.pdf",
        chunk_type=chunk_type,
        text=text,
        token_count=max(1, len(text.split())),
        page_start=1,
        page_end=2,
        heading_path=["Раздел", parent or "Подраздел"],
        section_title=parent,
        parent_section_id=parent,
        source_element_ids=[f"element-{chunk_id}"],
        previous_chunk_id=previous,
        next_chunk_id=next_,
        related_image_ids=images or [],
        related_table_ids=[f"table-{chunk_id}"] if chunk_type == "table" else [],
        source_type=source_type,
        metadata=metadata or {},
    )


def make_result(chunk: RetrievalChunk, rank: int, provenance: dict | None = None) -> SearchResult:
    return SearchResult(
        final_rank=rank,
        mode="rerank",
        final_score=1.0 - rank / 100,
        reranker_score=1.0 - rank / 100,
        rrf_rank=rank + 1,
        rrf_score=1 / (60 + rank),
        dense_rank=rank,
        dense_score=0.9,
        sparse_rank=rank + 2,
        sparse_score=8.0,
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        version_id=chunk.version_id,
        relative_path=chunk.relative_path,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        heading_path=chunk.heading_path,
        section_title=chunk.section_title,
        text=chunk.text,
        chunk_type=chunk.chunk_type,
        source_type=chunk.source_type,
        related_image_ids=chunk.related_image_ids,
        provenance=provenance or {},
        payload={"chunk_id": chunk.chunk_id},
    )


def builder(chunks: list[RetrievalChunk], **settings: object) -> ContextBuilder:
    return ContextBuilder(
        FakeChunkRepository(chunks),  # type: ignore[arg-type]
        FakeAssetRepository({"image-1"}),  # type: ignore[arg-type]
        settings=ContextBuilderSettings(**settings),
    )


def test_deduplicates_and_keeps_complementary_chunks_with_document_limit() -> None:
    base = " ".join(f"слово{i}" for i in range(40))
    chunks = [
        make_chunk("c1", base, parent="section-a"),
        make_chunk("c2", base.upper(), parent="section-a"),
        make_chunk("c3", base.replace("слово20", "замена"), parent="section-a"),
        make_chunk("c4", "Совершенно другой дополняющий фрагмент первого раздела.", parent="section-a"),
        make_chunk("c5", "Содержимое независимого второго раздела.", parent="section-b"),
        make_chunk("c6", "Четвёртый фрагмент того же документа.", parent="section-c"),
        make_chunk("c7", "Фрагмент другого документа.", document_id="doc-2"),
    ]
    package = builder(
        chunks,
        near_duplicate_threshold=0.75,
        max_chunks_per_document=3,
        neighbor_expansion_limit=0,
    ).build("вопрос", [make_result(chunk, rank) for rank, chunk in enumerate(chunks, 1)])

    assert [source.chunk_id for source in package.sources] == ["c1", "c4", "c5", "c7"]
    assert package.stats.exact_duplicates_removed == 1
    assert package.stats.near_duplicates_removed == 1
    assert package.stats.document_limit_removed == 1


def test_selective_neighbor_expansion_and_no_unnecessary_neighbor() -> None:
    primary = make_chunk("primary", "Ключевые условия:", next_="neighbor")
    neighbor = make_chunk(
        "neighbor", "Первое условие и его важное пояснение.", previous="primary"
    )
    complete = make_chunk(
        "complete", "Завершённый самостоятельный ответ.", document_id="doc-2", next_="unused"
    )
    unused = make_chunk(
        "unused", "Лишнее соседнее содержание.", document_id="doc-2", previous="complete"
    )
    package = builder([primary, neighbor, complete, unused]).build(
        "условия", [make_result(primary, 1), make_result(complete, 2)]
    )

    assert [source.chunk_id for source in package.sources] == ["primary", "neighbor", "complete"]
    assert package.sources[1].added_as_neighbor is True
    assert package.sources[1].parent_chunk_id == "primary"
    assert package.stats.neighbors_added == 1
    assert "unused" not in {source.chunk_id for source in package.sources}


def test_useful_neighbor_replaces_only_lower_rank_tail_when_context_is_full() -> None:
    primary = make_chunk("primary", "Порядок действий:", next_="neighbor")
    neighbor = make_chunk("neighbor", "Шаг один. Шаг два.", previous="primary")
    tail = make_chunk("tail", "Менее релевантный самостоятельный фрагмент.", document_id="doc-2")
    package = builder([primary, neighbor, tail], max_chunks=2).build(
        "порядок", [make_result(primary, 1), make_result(tail, 2)]
    )

    assert [source.chunk_id for source in package.sources] == ["primary", "neighbor"]
    assert package.stats.lower_rank_results_replaced_by_neighbors == 1
    assert package.stats.neighbors_added == 1


def test_token_budget_drops_lower_rank_without_cutting_chunk() -> None:
    first = make_chunk("first", " ".join(["важный"] * 35), document_id="doc-1")
    second = make_chunk("second", " ".join(["вторичный"] * 35), document_id="doc-2")
    package = builder(
        [first, second], max_context_tokens=300, neighbor_expansion_limit=0
    ).build("вопрос", [make_result(first, 1), make_result(second, 2)])

    assert [source.chunk_id for source in package.sources] == ["first"]
    assert package.sources[0].text == first.text
    assert package.stats.budget_removed == 1
    assert package.token_count <= 300


def test_table_images_provenance_numbering_determinism_and_no_input_mutation() -> None:
    table = make_chunk(
        "table",
        "Таблица ограничений",
        chunk_type="table",
        metadata={
            "table_headers": ["Параметр", "Значение"],
            "table_rows": [["Масса", "20 т"]],
            "table_html": "<table><tr><td>Масса</td><td>20 т</td></tr></table>",
            "row_start": 2,
            "row_end": 2,
            "source_fallback": "native_page_text",
        },
        images=["image-1", "missing-image"],
        source_type="mixed",
    )
    ocr = make_chunk(
        "ocr", "Распознанный текст", document_id="doc-2", source_type="ocr"
    )
    results = [
        make_result(table, 1, {"source_fallback": "native_page_text"}),
        make_result(ocr, 2, {"ocr_provenance": [{"ocr_source": "imported"}]}),
    ]
    before = deepcopy([result.model_dump(mode="json") for result in results])
    context_builder = builder([table, ocr], neighbor_expansion_limit=0)

    first = context_builder.build("ограничение", results)
    second = context_builder.build("ограничение", results)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert [source.citation for source in first.sources] == ["[1]", "[2]"]
    assert first.sources[0].table is not None
    assert first.sources[0].table.headers == ["Параметр", "Значение"]
    assert first.sources[0].table.rows == [["Масса", "20 т"]]
    assert first.sources[0].provenance["source_fallback"] == "native_page_text"
    assert first.sources[1].provenance["ocr_provenance"][0]["ocr_source"] == "imported"
    assert first.sources[0].related_image_ids == ["image-1"]
    assert [image.asset_id for image in first.related_images] == ["image-1"]
    assert first.stats.missing_assets_removed == 1
    assert [result.model_dump(mode="json") for result in results] == before
    assert "Параметр: Масса" in first.rendered_context
    assert "Значение: 20 т" in first.rendered_context
    assert first.sources[0].table.html == table.metadata["table_html"]
    assert first.sources[0].table.render_headers == ["Параметр", "Значение"]


def test_asset_repository_returns_only_declared_assets_with_existing_files(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    normalized = storage / "normalized" / "doc-1"
    assets = storage / "assets" / "doc-1"
    normalized.mkdir(parents=True)
    assets.mkdir(parents=True)
    (assets / "present.png").write_bytes(b"png")
    document = NormalizedDocument(
        document_id="doc-1",
        version_id="version-1",
        assets=[
            NormalizedAsset(
                asset_id="present",
                type="image",
                local_path="assets/doc-1/present.png",
            ),
            NormalizedAsset(
                asset_id="missing-file",
                type="image",
                local_path="assets/doc-1/missing.png",
            ),
        ],
    )
    (normalized / "version-1.json").write_text(
        document.model_dump_json(), encoding="utf-8"
    )

    found, missing = AssetRepository(storage / "normalized").resolve(
        "doc-1", "version-1", ["present", "missing-file", "undeclared"]
    )

    assert [image.asset_id for image in found] == ["present"]
    assert missing == ["missing-file", "undeclared"]


def test_asset_repository_resolves_standalone_visual_from_knowledge_base(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    knowledge = tmp_path / "knowledge"
    normalized = storage / "normalized" / "doc-visual"
    visual = knowledge / "folder" / "visual.png"
    normalized.mkdir(parents=True)
    visual.parent.mkdir(parents=True)
    visual.write_bytes(b"png")
    document = NormalizedDocument(
        document_id="doc-visual",
        version_id="version-1",
        assets=[
            NormalizedAsset(
                asset_id="visual",
                type="image",
                local_path="folder/visual.png",
                metadata={"path_scope": "knowledge_base"},
            )
        ],
    )
    (normalized / "version-1.json").write_text(document.model_dump_json(), encoding="utf-8")

    found, missing = AssetRepository(storage / "normalized", knowledge).resolve(
        "doc-visual", "version-1", ["visual"]
    )

    assert missing == []
    assert found[0].asset_id == "visual"
    assert found[0].path_scope == "knowledge_base"
