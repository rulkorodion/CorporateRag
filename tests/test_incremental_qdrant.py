from __future__ import annotations

import uuid
from pathlib import Path

from corporate_rag_ingestion.chunking.models import ChunkCoverage, ChunkedDocument, RetrievalChunk
from corporate_rag_ingestion.retrieval.indexer import HybridIndexer
from corporate_rag_ingestion.retrieval.models import EmbeddingRuntime, SparseEmbedding
from corporate_rag_ingestion.retrieval.settings import RetrievalSettings
from corporate_rag_ingestion.retrieval.store import (
    INCREMENTAL_CANDIDATE_FIELD,
    QdrantStore,
)


class Dense:
    def __init__(self, settings: RetrievalSettings) -> None:
        self.settings = settings

    @property
    def runtime(self) -> EmbeddingRuntime:
        return EmbeddingRuntime(
            model_id="fake",
            model_revision="test",
            dimension=2,
            device="cpu",
            dtype="float32",
            settings_hash=self.settings.dense_settings_hash,
        )

    def embed_documents(self, texts):  # type: ignore[no-untyped-def]
        return [[1.0, 0.0] for _ in texts]

    def token_counts(self, texts):  # type: ignore[no-untyped-def]
        return [len(text.split()) for text in texts]


class Sparse:
    def embed_documents(self, texts):  # type: ignore[no-untyped-def]
        return [SparseEmbedding(indices=[1], values=[1.0]) for _ in texts]


def document(
    version: str,
    text: str,
    *,
    document_id: str = "11111111-1111-5111-8111-111111111111",
    relative_path: str = "policy.docx",
) -> ChunkedDocument:
    chunk = RetrievalChunk(
        chunk_id=str(uuid.uuid5(uuid.UUID(document_id), version)),
        document_id=document_id,
        version_id=version,
        relative_path=relative_path,
        chunk_type="text",
        text=text,
        token_count=2,
        source_element_ids=[f"{version}:element"],
        source_type="native",
    )
    return ChunkedDocument(
        document_id=document_id,
        version_id=version,
        relative_path=relative_path,
        source_sha256="a" * 64,
        chunker_version="test",
        settings_hash="b" * 64,
        tokenizer_method="test",
        chunks=[chunk],
        coverage=ChunkCoverage(
            eligible_source_elements=1,
            covered_source_elements=1,
        ),
    )


def test_controlled_document_switch_and_rollback(tmp_path: Path) -> None:
    settings = RetrievalSettings(
        _env_file=None,
        qdrant_path=tmp_path / "qdrant",
        collection_name="incremental-switch",
        dense_dimension=2,
        dense_model_id="fake",
        dense_model_revision="test",
        sparse_model_id="fake",
        sparse_model_revision="test",
        sparse_cache_path=tmp_path / "sparse",
        model_cache_path=tmp_path / "models",
    )
    store = QdrantStore(settings)
    indexer = HybridIndexer(settings, store, Dense(settings), Sparse())  # type: ignore[arg-type]
    try:
        store.ensure_collection()
        active = document("version-1", "active text")
        active_prepared = indexer.prepare_document(active)
        store.controlled_replace_document(active.document_id, active.version_id, active_prepared.records)
        unrelated = document(
            "other-version",
            "must remain untouched",
            document_id="22222222-2222-5222-8222-222222222222",
            relative_path="other.docx",
        )
        unrelated_prepared = indexer.prepare_document(unrelated)
        store.controlled_replace_document(
            unrelated.document_id,
            unrelated.version_id,
            unrelated_prepared.records,
        )

        candidate = document("version-2", "candidate text")
        candidate_prepared = indexer.prepare_document(candidate)
        switch = store.controlled_replace_document(
            candidate.document_id,
            candidate.version_id,
            candidate_prepared.records,
        )
        switched = store.payloads_for_document(candidate.document_id)
        assert {payload["version_id"] for payload in switched.values()} == {"version-2"}
        assert all(
            payload[INCREMENTAL_CANDIDATE_FIELD] is False
            for payload in switched.values()
        )

        hidden = document(
            "hidden-version",
            "must never be searchable",
            document_id="33333333-3333-5333-8333-333333333333",
            relative_path="hidden.docx",
        )
        hidden_record = indexer.prepare_document(hidden).records[0]
        point_id, dense, sparse, payload = hidden_record
        store.upsert(
            [
                (
                    point_id,
                    dense,
                    sparse,
                    {**payload, INCREMENTAL_CANDIDATE_FIELD: True},
                )
            ]
        )
        searchable = store.query_dense([1.0, 0.0], limit=20)
        assert point_id not in {str(point.id) for point in searchable}

        store.rollback_document_switch(switch)
        restored = store.payloads_for_document(candidate.document_id)
        assert {payload["version_id"] for payload in restored.values()} == {"version-1"}
        assert next(iter(restored.values()))["text"] == "active text"
        other = store.payloads_for_document(unrelated.document_id)
        assert len(other) == 1
        assert next(iter(other.values()))["text"] == "must remain untouched"
    finally:
        store.close()
