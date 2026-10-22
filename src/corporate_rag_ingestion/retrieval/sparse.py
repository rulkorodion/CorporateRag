from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .models import SparseEmbedding
from .settings import RetrievalSettings


class SparseEmbeddingBackend(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> list[SparseEmbedding]: ...

    def embed_query(self, text: str) -> SparseEmbedding: ...


class BM25SparseEmbedder:
    """FastEmbed Qdrant/BM25 with Russian stemming; IDF is applied by Qdrant."""

    def __init__(self, settings: RetrievalSettings) -> None:
        self.settings = settings
        self._model = None

    def _load(self):  # type: ignore[no-untyped-def]
        if self._model is None:
            from fastembed import SparseTextEmbedding
            from huggingface_hub import snapshot_download

            self.settings.sparse_cache_path.mkdir(parents=True, exist_ok=True)
            snapshot = snapshot_download(
                repo_id=self.settings.sparse_model_id,
                revision=self.settings.sparse_model_revision,
                cache_dir=str(self.settings.sparse_cache_path),
                local_files_only=self.settings.sparse_local_files_only,
            )
            self._model = SparseTextEmbedding(
                model_name=self.settings.sparse_model_id,
                cache_dir=str(self.settings.sparse_cache_path),
                language=self.settings.sparse_language,
                specific_model_path=snapshot,
                lazy_load=False,
            )
        return self._model

    @staticmethod
    def _convert(value) -> SparseEmbedding:  # type: ignore[no-untyped-def]
        return SparseEmbedding(
            indices=[int(item) for item in value.indices.tolist()],
            values=[float(item) for item in value.values.tolist()],
        )

    def embed_documents(self, texts: Sequence[str]) -> list[SparseEmbedding]:
        if not texts:
            return []
        return [self._convert(item) for item in self._load().embed(list(texts))]

    def embed_query(self, text: str) -> SparseEmbedding:
        return self._convert(next(iter(self._load().query_embed(text))))
