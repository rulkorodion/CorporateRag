from __future__ import annotations

import atexit
import threading
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..config import PROJECT_ROOT, Settings
from ..context.builder import ContextBuilder
from ..context.models import ContextPackage
from ..context.repository import AssetRepository, ChunkRepository
from ..context.settings import ContextBuilderSettings
from ..generation.models import RAGAnswer
from ..generation.ollama import OllamaClient
from ..generation.service import RAGAnswerService
from ..generation.settings import GenerationSettings
from ..retrieval.dense import QwenDenseEmbedder
from ..retrieval.rerank_service import RerankSearchService
from ..retrieval.reranker import QwenCrossEncoderReranker
from ..retrieval.search import HybridSearchService
from ..retrieval.settings import RetrievalSettings
from ..retrieval.sparse import BM25SparseEmbedder
from ..retrieval.store import QdrantStore
from .presentation import NormalizedPresentationRepository


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_limit: int = Field(default=40, gt=0)
    top_k: int = Field(default=8, gt=0)
    max_context_tokens: int = Field(default=6000, gt=0)
    max_chunks: int = Field(default=8, gt=0)
    max_chunks_per_document: int = Field(default=3, gt=0)
    neighbor_expansion_limit: int = Field(default=2, ge=0)
    dense_device: str = "cpu"
    reranker_device: str = "auto"


class PersistentRAGRuntime:
    """Single-process RAG runtime whose model instances survive UI reruns."""

    def __init__(
        self,
        *,
        project_settings: Settings | None = None,
        retrieval_settings: RetrievalSettings | None = None,
        generation_settings: GenerationSettings | None = None,
        runtime_settings: RuntimeSettings | None = None,
    ) -> None:
        self.project_settings = project_settings or Settings()
        self.runtime_settings = runtime_settings or RuntimeSettings()
        base_retrieval = retrieval_settings or RetrievalSettings()
        self.retrieval_settings = base_retrieval.model_copy(
            update={
                "dense_device": self.runtime_settings.dense_device,
                "reranker_device": self.runtime_settings.reranker_device,
                "reranker_release_dense_before_load": False,
            }
        ).resolve_paths(PROJECT_ROOT)
        self.generation_settings = generation_settings or GenerationSettings()
        self.store = QdrantStore(self.retrieval_settings)
        self.dense = QwenDenseEmbedder(self.retrieval_settings)
        self.sparse = BM25SparseEmbedder(self.retrieval_settings)
        self.hybrid = HybridSearchService(
            self.retrieval_settings, self.store, self.dense, self.sparse
        )
        self.reranker = QwenCrossEncoderReranker(self.retrieval_settings)
        self.rerank_service = RerankSearchService(
            self.retrieval_settings,
            self.hybrid,
            self.reranker,
            release_candidate_backend=None,
        )
        self.chunks = ChunkRepository(self.project_settings.chunk_storage_root)
        self.assets = AssetRepository(
            self.project_settings.normalized_root,
            self.project_settings.knowledge_base_root,
        )
        self.ollama = OllamaClient(self.generation_settings)
        self.generator = RAGAnswerService(self.ollama, self.generation_settings)
        self.presentation = NormalizedPresentationRepository(
            self.project_settings.normalized_root
        )
        self._lock = threading.Lock()
        self._closed = False
        atexit.register(self.close)

    def warmup(self) -> None:
        """Load both local models once; dense stays on CPU and reranker on CUDA."""

        with self._lock:
            self.dense.embed_query("проверка готовности поиска")
            _ = self.reranker.runtime

    def answer(
        self,
        query: str,
        *,
        candidate_limit: int | None = None,
        top_k: int | None = None,
        max_context_tokens: int | None = None,
        max_chunks: int | None = None,
    ) -> tuple[RAGAnswer, ContextPackage]:
        query = query.strip()
        if not query:
            raise ValueError("question cannot be empty")
        candidates = candidate_limit or self.runtime_settings.candidate_limit
        final_top_k = top_k or self.runtime_settings.top_k
        context_chunks = max_chunks or self.runtime_settings.max_chunks
        if final_top_k > candidates:
            raise ValueError("top_k cannot exceed candidate_limit")
        with self._lock:
            started = time.perf_counter()
            reranked = self.rerank_service.search(
                query,
                top_k=final_top_k,
                candidate_limit=candidates,
            )
            context = ContextBuilder(
                self.chunks,
                self.assets,
                settings=ContextBuilderSettings(
                    max_context_tokens=(
                        max_context_tokens or self.runtime_settings.max_context_tokens
                    ),
                    max_chunks=context_chunks,
                    max_chunks_per_document=self.runtime_settings.max_chunks_per_document,
                    neighbor_expansion_limit=self.runtime_settings.neighbor_expansion_limit,
                ),
            ).build(query, reranked)
            upstream_seconds = time.perf_counter() - started
            answer = self.generator.generate(
                context, retrieval_context_seconds=upstream_seconds
            )
            answer = self.presentation.enrich_attachments(answer)
            return answer, context

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.ollama.close()
        self.store.close()

    @property
    def knowledge_base_root(self) -> Path:
        return self.project_settings.knowledge_base_root

    @property
    def storage_root(self) -> Path:
        return self.project_settings.storage_root
