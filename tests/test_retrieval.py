from __future__ import annotations

import hashlib
import math
import re
import uuid
from pathlib import Path

from corporate_rag_ingestion.chunking.models import (
    ChunkCoverage,
    ChunkedDocument,
    RetrievalChunk,
)
from corporate_rag_ingestion.retrieval.indexer import HybridIndexer
from corporate_rag_ingestion.retrieval.models import EmbeddingRuntime, SparseEmbedding
from corporate_rag_ingestion.retrieval.search import HybridSearchService
from corporate_rag_ingestion.retrieval.settings import RetrievalSettings
from corporate_rag_ingestion.retrieval.store import QdrantStore


class FakeDense:
    def __init__(self, settings: RetrievalSettings) -> None:
        self.settings = settings
        self.calls = 0

    @property
    def runtime(self) -> EmbeddingRuntime:
        return EmbeddingRuntime(
            model_id="fake-dense",
            model_revision="test",
            dimension=4,
            device="cpu",
            dtype="float32",
            settings_hash=self.settings.dense_settings_hash,
        )

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.casefold()
        vector = [
            float(any(word in lowered for word in ("достав", "автомоб"))),
            float(any(word in lowered for word in ("гост", "ту 24"))),
            float(any(word in lowered for word in ("ocr", "служебн"))),
            0.1,
        ]
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]

    def embed_documents(self, texts):  # type: ignore[no-untyped-def]
        self.calls += len(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def token_counts(self, texts):  # type: ignore[no-untyped-def]
        return [len(text.split()) + 2 for text in texts]


class FakeSparse:
    @staticmethod
    def _vector(text: str) -> SparseEmbedding:
        counts: dict[int, float] = {}
        for token in re.findall(r"[\w.-]+", text.casefold()):
            index = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "big")
            counts[index] = counts.get(index, 0.0) + 1.0
        return SparseEmbedding(indices=sorted(counts), values=[counts[key] for key in sorted(counts)])

    def embed_documents(self, texts):  # type: ignore[no-untyped-def]
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> SparseEmbedding:
        return self._vector(text)


def make_settings(tmp_path: Path) -> RetrievalSettings:
    return RetrievalSettings(
        qdrant_path=tmp_path / "qdrant",
        collection_name="test_chunks",
        dense_model_id="fake",
        dense_model_revision="test",
        dense_dimension=4,
        dense_max_length=128,
        sparse_model_id="fake-bm25",
        sparse_cache_path=tmp_path / "sparse",
        model_cache_path=tmp_path / "models",
        candidate_limit=10,
    )


def test_local_model_paths_resolve_without_changing_index_identity(tmp_path: Path) -> None:
    baseline = RetrievalSettings(_env_file=None)
    local = RetrievalSettings(
        _env_file=None,
        dense_model_path=Path("models/embedding"),
        reranker_model_path=Path("models/reranker"),
    ).resolve_paths(tmp_path)

    assert local.dense_model_path == (tmp_path / "models/embedding").resolve()
    assert local.reranker_model_path == (tmp_path / "models/reranker").resolve()
    assert local.dense_model_id == baseline.dense_model_id
    assert local.dense_model_revision == baseline.dense_model_revision
    assert local.reranker_model_id == baseline.reranker_model_id
    assert local.reranker_model_revision == baseline.reranker_model_revision
    assert local.index_settings_hash == baseline.index_settings_hash


def make_chunk(
    text: str,
    *,
    chunk_id: str | None = None,
    metadata: dict | None = None,
    source_type: str = "native",
    images: list[str] | None = None,
) -> RetrievalChunk:
    return RetrievalChunk(
        chunk_id=chunk_id or str(uuid.uuid4()),
        document_id="11111111-1111-5111-8111-111111111111",
        version_id="version-1",
        relative_path="folder/test.pdf",
        chunk_type="text",
        text=text,
        token_count=max(1, len(text.split())),
        page_start=1,
        page_end=1,
        heading_path=["Раздел"],
        section_title="Раздел",
        source_element_ids=[str(uuid.uuid4())],
        source_type=source_type,
        related_image_ids=images or [],
        metadata=metadata or {},
    )


def save_document(root: Path, chunks: list[RetrievalChunk]) -> None:
    for index, chunk in enumerate(chunks):
        chunk.previous_chunk_id = chunks[index - 1].chunk_id if index else None
        chunk.next_chunk_id = chunks[index + 1].chunk_id if index + 1 < len(chunks) else None
    document = ChunkedDocument(
        document_id="11111111-1111-5111-8111-111111111111",
        version_id="version-1",
        relative_path="folder/test.pdf",
        source_sha256="a" * 64,
        chunker_version="structural-v1.1",
        settings_hash="b" * 64,
        tokenizer_method="test",
        chunks=chunks,
        coverage=ChunkCoverage(
            eligible_source_elements=len(chunks),
            covered_source_elements=len(chunks),
        ),
    )
    path = root / "doc" / "version.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document.model_dump_json(indent=2), encoding="utf-8")


def test_index_exclusion_idempotency_payload_and_provenance(tmp_path: Path) -> None:
    root = tmp_path / "chunks"
    included = make_chunk(
        "Максимальная загрузка автомобиля 20 тонн",
        metadata={
            "source_fallback": "native_page_text",
            "source_fallback_reason": "docling_structural_elements_missing_for_page",
        },
        images=["image-1"],
    )
    excluded = make_chunk("---", metadata={"retrieval_candidate": False})
    save_document(root, [included, excluded])
    settings = make_settings(tmp_path)
    store = QdrantStore(settings)
    dense = FakeDense(settings)
    try:
        first = HybridIndexer(settings, store, dense, FakeSparse()).run(root)
        second = HybridIndexer(settings, store, dense, FakeSparse()).run(root)
        payload = store.payloads()[included.chunk_id]
        collection = store.client.get_collection(settings.collection_name)
    finally:
        store.close()

    assert first.total_chunks == 2 and first.indexable_chunks == 1
    assert first.excluded_chunks == 1 and first.collection_points == 1
    assert second.embedded_chunks == 0 and second.unchanged_chunks == 1
    assert dense.calls == 1
    assert payload["related_image_ids"] == ["image-1"]
    assert payload["provenance"]["source_fallback"] == "native_page_text"
    assert payload["chunker_version"] == "structural-v1.1"
    assert payload["source_element_ids"] == included.source_element_ids
    assert collection.config.metadata["index_settings_hash"] == settings.index_settings_hash


def test_dense_sparse_hybrid_and_metadata_filter(tmp_path: Path) -> None:
    root = tmp_path / "chunks"
    delivery = make_chunk("Максимальная загрузка автомобиля составляет 20 тонн")
    standard = make_chunk("ТУ 24.20.13-004-40198874-2020 труба водогазопроводная")
    ocr = make_chunk(
        "OCR служебная записка об испытаниях",
        source_type="ocr",
        metadata={"ocr_provenance": [{"ocr_source": "imported", "page_number": 1}]},
    )
    save_document(root, [delivery, standard, ocr])
    settings = make_settings(tmp_path)
    store = QdrantStore(settings)
    dense = FakeDense(settings)
    sparse = FakeSparse()
    try:
        HybridIndexer(settings, store, dense, sparse).run(root)
        service = HybridSearchService(settings, store, dense, sparse)
        dense_hits = service.search("сколько можно загрузить в автомобиль", mode="dense", top_k=2)
        sparse_hits = service.search("ТУ 24.20.13-004-40198874-2020", mode="sparse", top_k=2)
        hybrid_hits = service.search("максимальная загрузка автомобиля", mode="hybrid", top_k=2)
        filtered = service.search(
            "служебная записка", mode="hybrid", top_k=3, filters={"source_type": "ocr"}
        )
    finally:
        store.close()

    assert dense_hits[0].chunk_id == delivery.chunk_id
    assert sparse_hits[0].chunk_id == standard.chunk_id
    assert hybrid_hits[0].dense_rank is not None and hybrid_hits[0].sparse_rank is not None
    assert hybrid_hits[0].rrf_score is not None
    assert [result.final_rank for result in hybrid_hits] == [1, 2]
    assert [result.chunk_id for result in filtered] == [ocr.chunk_id]
    assert filtered[0].payload["provenance"]["ocr_provenance"][0]["ocr_source"] == "imported"


def test_changed_chunk_updates_and_removed_chunk_is_deleted(tmp_path: Path) -> None:
    root = tmp_path / "chunks"
    stable_id = str(uuid.uuid4())
    first = make_chunk("Первоначальный текст доставки", chunk_id=stable_id)
    removed = make_chunk("Устаревший ГОСТ")
    save_document(root, [first, removed])
    settings = make_settings(tmp_path)
    store = QdrantStore(settings)
    dense = FakeDense(settings)
    try:
        HybridIndexer(settings, store, dense, FakeSparse()).run(root)
        changed = make_chunk("Изменённый текст доставки", chunk_id=stable_id)
        save_document(root, [changed])
        summary = HybridIndexer(settings, store, dense, FakeSparse()).run(root)
        payloads = store.payloads()
    finally:
        store.close()

    assert summary.embedded_chunks == 1
    assert summary.upserted_chunks == 1
    assert summary.deleted_chunks == 1
    assert set(payloads) == {stable_id}
    assert payloads[stable_id]["text"] == "Изменённый текст доставки"
