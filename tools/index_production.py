from __future__ import annotations

"""Synchronize production chunks into an already running Qdrant Server.

This deliberately imports only the retrieval layer, so the production image
does not need ingestion/Docling dependencies.
"""

import json

from corporate_rag_ingestion.config import PROJECT_ROOT, Settings, configure_logging
from corporate_rag_ingestion.retrieval.dense import QwenDenseEmbedder
from corporate_rag_ingestion.retrieval.indexer import HybridIndexer
from corporate_rag_ingestion.retrieval.settings import RetrievalSettings
from corporate_rag_ingestion.retrieval.sparse import BM25SparseEmbedder
from corporate_rag_ingestion.retrieval.store import QdrantStore


def main() -> int:
    project = Settings()
    configure_logging(project.log_level)
    retrieval = RetrievalSettings().resolve_paths(PROJECT_ROOT)
    if not retrieval.qdrant_url:
        raise RuntimeError("RETRIEVAL_QDRANT_URL is required for production indexing")
    store = QdrantStore(retrieval)
    dense = QwenDenseEmbedder(retrieval)
    sparse = BM25SparseEmbedder(retrieval)
    try:
        summary = HybridIndexer(retrieval, store, dense, sparse).run(
            project.chunk_storage_root
        )
        print(json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 1 if summary.errors else 0
    finally:
        dense.release()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
