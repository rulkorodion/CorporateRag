from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..chunking.models import ChunkedDocument, RetrievalChunk
from .dense import DenseEmbeddingBackend
from .models import IndexSummary, TokenCountComparison
from .settings import RetrievalSettings
from .sparse import SparseEmbeddingBackend
from .store import QdrantStore


@dataclass(slots=True)
class PreparedDocumentIndex:
    records: list[tuple[str, list[float], Any, dict[str, Any]]]
    total_chunks: int
    indexable_chunks: int
    excluded_chunks: int
    embedding_seconds: float
    sparse_seconds: float


class HybridIndexer:
    def __init__(
        self,
        settings: RetrievalSettings,
        store: QdrantStore,
        dense: DenseEmbeddingBackend,
        sparse: SparseEmbeddingBackend,
    ) -> None:
        self.settings = settings
        self.store = store
        self.dense = dense
        self.sparse = sparse

    def run(self, chunks_root: Path) -> IndexSummary:
        started = time.perf_counter()
        self.store.ensure_collection()
        documents = [
            ChunkedDocument.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(chunks_root.rglob("*.json"))
        ]
        all_chunks = [(document, chunk) for document in documents for chunk in document.chunks]
        indexable = [
            (document, chunk)
            for document, chunk in all_chunks
            if chunk.metadata.get("retrieval_candidate") is not False
        ]
        existing = self.store.payloads()
        desired: dict[str, tuple[ChunkedDocument, RetrievalChunk, str]] = {}
        for document, chunk in indexable:
            fingerprint = self._fingerprint(document, chunk)
            desired[chunk.chunk_id] = (document, chunk, fingerprint)
        changed = [
            value
            for chunk_id, value in desired.items()
            if existing.get(chunk_id, {}).get("index_fingerprint") != value[2]
        ]
        stale_ids = sorted(set(existing) - set(desired))

        embedding_seconds = 0.0
        sparse_seconds = 0.0
        upsert_seconds = 0.0
        changed_records: list[tuple[str, list[float], Any, dict[str, Any]]] = []
        if changed:
            texts = [chunk.text for _, chunk, _ in changed]
            dense_started = time.perf_counter()
            dense_vectors = self.dense.embed_documents(texts)
            model_token_counts = self.dense.token_counts(texts)
            embedding_seconds = time.perf_counter() - dense_started
            sparse_started = time.perf_counter()
            sparse_vectors = self.sparse.embed_documents(texts)
            sparse_seconds = time.perf_counter() - sparse_started
            runtime = self.dense.runtime
            for (document, chunk, fingerprint), dense, sparse, model_tokens in zip(
                changed,
                dense_vectors,
                sparse_vectors,
                model_token_counts,
                strict=True,
            ):
                payload = self._payload(
                    document,
                    chunk,
                    fingerprint,
                    runtime.model_dump(mode="json"),
                    model_tokens,
                )
                changed_records.append((chunk.chunk_id, dense, sparse, payload))
            upsert_started = time.perf_counter()
            for start in range(0, len(changed_records), self.settings.upsert_batch_size):
                self.store.upsert(changed_records[start : start + self.settings.upsert_batch_size])
            upsert_seconds = time.perf_counter() - upsert_started
        else:
            sample = next(iter(existing.values()), None)
            if sample is None or not sample.get("embedding_runtime"):
                runtime = self.dense.runtime
            else:
                from .models import EmbeddingRuntime

                runtime = EmbeddingRuntime.model_validate(sample["embedding_runtime"])

        self.store.delete(stale_ids)
        # Collection metadata is committed only after every point mutation has
        # completed, so it never advertises settings that were not fully built.
        self.store.set_index_metadata()
        current_payloads = self.store.payloads()
        structural_counts = [chunk.token_count for _, chunk in indexable]
        model_counts = [
            int(current_payloads[chunk.chunk_id]["model_token_count"])
            for _, chunk in indexable
        ]
        token_comparison = self._token_comparison(
            structural_counts,
            model_counts,
            max_length=self.settings.dense_max_length,
        )
        return IndexSummary(
            collection_name=self.settings.collection_name,
            total_chunks=len(all_chunks),
            indexable_chunks=len(indexable),
            excluded_chunks=len(all_chunks) - len(indexable),
            unchanged_chunks=len(indexable) - len(changed),
            embedded_chunks=len(changed),
            upserted_chunks=len(changed_records),
            deleted_chunks=len(stale_ids),
            collection_points=self.store.count(),
            embedding_seconds=round(embedding_seconds, 3),
            sparse_seconds=round(sparse_seconds, 3),
            upsert_seconds=round(upsert_seconds, 3),
            total_seconds=round(time.perf_counter() - started, 3),
            storage_bytes=self.store.storage_bytes(),
            runtime=runtime,
            token_counts=token_comparison,
        )

    def prepare_document(self, document: ChunkedDocument) -> PreparedDocumentIndex:
        """Calculate vectors/payload only for one fully prepared document version."""

        indexable = [
            chunk
            for chunk in document.chunks
            if chunk.metadata.get("retrieval_candidate") is not False
        ]
        if not indexable:
            return PreparedDocumentIndex(
                records=[],
                total_chunks=len(document.chunks),
                indexable_chunks=0,
                excluded_chunks=len(document.chunks),
                embedding_seconds=0.0,
                sparse_seconds=0.0,
            )

        texts = [chunk.text for chunk in indexable]
        dense_started = time.perf_counter()
        dense_vectors = self.dense.embed_documents(texts)
        model_token_counts = self.dense.token_counts(texts)
        embedding_seconds = time.perf_counter() - dense_started
        sparse_started = time.perf_counter()
        sparse_vectors = self.sparse.embed_documents(texts)
        sparse_seconds = time.perf_counter() - sparse_started
        runtime = self.dense.runtime.model_dump(mode="json")
        records = []
        for chunk, dense, sparse, model_tokens in zip(
            indexable,
            dense_vectors,
            sparse_vectors,
            model_token_counts,
            strict=True,
        ):
            fingerprint = self._fingerprint(document, chunk)
            records.append(
                (
                    chunk.chunk_id,
                    dense,
                    sparse,
                    self._payload(document, chunk, fingerprint, runtime, model_tokens),
                )
            )
        return PreparedDocumentIndex(
            records=records,
            total_chunks=len(document.chunks),
            indexable_chunks=len(indexable),
            excluded_chunks=len(document.chunks) - len(indexable),
            embedding_seconds=embedding_seconds,
            sparse_seconds=sparse_seconds,
        )

    def _fingerprint(self, document: ChunkedDocument, chunk: RetrievalChunk) -> str:
        payload = {
            "chunk": chunk.model_dump(mode="json"),
            "chunker_version": document.chunker_version,
            "chunk_settings_hash": document.settings_hash,
            "index_settings_hash": self.settings.index_settings_hash,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    def _payload(
        self,
        document: ChunkedDocument,
        chunk: RetrievalChunk,
        fingerprint: str,
        runtime: dict[str, Any],
        model_token_count: int,
    ) -> dict[str, Any]:
        return {
            "chunk_id": chunk.chunk_id,
            "document_id": chunk.document_id,
            "version_id": chunk.version_id,
            "relative_path": chunk.relative_path,
            "chunk_type": chunk.chunk_type,
            "text": chunk.text,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "heading_path": chunk.heading_path,
            "section_title": chunk.section_title,
            "parent_section_id": chunk.parent_section_id,
            "source_type": chunk.source_type,
            "related_image_ids": chunk.related_image_ids,
            "related_table_ids": chunk.related_table_ids,
            "source_element_ids": chunk.source_element_ids,
            "chunk_metadata": chunk.metadata,
            "provenance": {
                key: value
                for key, value in chunk.metadata.items()
                if key.startswith("ocr_")
                or key.startswith("source_fallback")
                or key.startswith("fallback_")
            },
            "chunker_version": document.chunker_version,
            "chunk_settings_hash": document.settings_hash,
            "index_schema_version": self.settings.index_schema_version,
            "index_settings_hash": self.settings.index_settings_hash,
            "index_fingerprint": fingerprint,
            "embedding_runtime": runtime,
            "embedding_settings_hash": self.settings.dense_settings_hash,
            "sparse_model_id": self.settings.sparse_model_id,
            "sparse_settings_hash": self.settings.sparse_settings_hash,
            "structural_token_count": chunk.token_count,
            "model_token_count": model_token_count,
        }

    def _token_comparison(
        self,
        structural: list[int],
        model: list[int],
        *,
        max_length: int,
    ) -> TokenCountComparison:
        def percentile(values: list[int], fraction: float) -> int:
            ordered = sorted(values)
            return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else 0

        ratio = [actual / max(1, estimated) for estimated, actual in zip(structural, model)]
        return TokenCountComparison(
            structural_min=min(structural, default=0),
            structural_avg=round(statistics.fmean(structural), 2) if structural else 0.0,
            structural_median=round(statistics.median(structural), 2) if structural else 0.0,
            structural_p95=percentile(structural, 0.95),
            structural_max=max(structural, default=0),
            model_min=min(model, default=0),
            model_avg=round(statistics.fmean(model), 2) if model else 0.0,
            model_median=round(statistics.median(model), 2) if model else 0.0,
            model_p95=percentile(model, 0.95),
            model_max=max(model, default=0),
            mean_ratio=round(statistics.fmean(ratio), 3) if ratio else 0.0,
            truncated=sum(value > max_length for value in model),
        )
