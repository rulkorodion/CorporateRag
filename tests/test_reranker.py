from __future__ import annotations

from pathlib import Path

import pytest

from corporate_rag_ingestion.retrieval.models import RerankerRuntime, SearchResult
from corporate_rag_ingestion.retrieval.rerank_service import (
    RerankerUnavailableError,
    RerankSearchService,
)
from corporate_rag_ingestion.retrieval.reranker import QwenCrossEncoderReranker
from corporate_rag_ingestion.retrieval.settings import RetrievalSettings


class FakeCandidateRetriever:
    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results
        self.calls: list[dict] = []

    def search(self, query: str, *, mode: str, top_k: int, filters=None):  # type: ignore[no-untyped-def]
        self.calls.append(
            {"query": query, "mode": mode, "top_k": top_k, "filters": filters}
        )
        return self.results[:top_k]


class FakeReranker:
    def __init__(self, scores: list[float] | None = None, error: Exception | None = None) -> None:
        self.scores = scores or []
        self.error = error
        self.calls: list[tuple[str, list[str]]] = []

    @property
    def runtime(self) -> RerankerRuntime:
        return RerankerRuntime(
            model_id="fake",
            model_revision="test",
            device="cpu",
            dtype="float32",
            batch_size=2,
            max_length=128,
            settings_hash="a" * 64,
        )

    def score(self, query: str, documents):  # type: ignore[no-untyped-def]
        self.calls.append((query, list(documents)))
        if self.error is not None:
            raise self.error
        return self.scores

    def release(self) -> None:
        pass


def make_result(
    rank: int,
    *,
    text: str,
    source_type: str = "native",
    images: list[str] | None = None,
    provenance: dict | None = None,
) -> SearchResult:
    payload = {
        "chunk_id": f"chunk-{rank}",
        "provenance": provenance or {},
    }
    return SearchResult(
        final_rank=rank,
        mode="hybrid",
        final_score=1 / (60 + rank),
        rrf_rank=rank,
        rrf_score=1 / (60 + rank),
        dense_rank=rank,
        dense_score=0.9 - rank / 100,
        sparse_rank=rank + 1,
        sparse_score=10.0 - rank,
        chunk_id=f"chunk-{rank}",
        document_id="doc-1",
        version_id="version-1",
        relative_path="folder/document.pdf",
        page_start=rank,
        page_end=rank,
        heading_path=["Раздел", f"Подраздел {rank}"],
        section_title=f"Подраздел {rank}",
        text=text,
        chunk_type="text",
        source_type=source_type,
        related_image_ids=images or [],
        provenance=provenance or {},
        payload=payload,
    )


def make_settings(tmp_path: Path) -> RetrievalSettings:
    return RetrievalSettings(
        qdrant_path=tmp_path / "qdrant",
        model_cache_path=tmp_path / "models",
        reranker_candidate_limit=3,
        reranker_final_top_k=2,
    )


def test_rerank_uses_limited_hybrid_candidates_and_sorts_scores(tmp_path: Path) -> None:
    candidates = [
        make_result(1, text="Первый"),
        make_result(
            2,
            text="OCR второй",
            source_type="ocr",
            images=["image-2"],
            provenance={"ocr_provenance": [{"ocr_source": "imported"}]},
        ),
        make_result(
            3,
            text="Fallback третий",
            provenance={"source_fallback": "native_page_text"},
        ),
        make_result(4, text="Не должен попасть в candidates"),
    ]
    retriever = FakeCandidateRetriever(candidates)
    reranker = FakeReranker([0.2, 0.95, 0.6])
    released: list[bool] = []
    service = RerankSearchService(
        make_settings(tmp_path),
        retriever,
        reranker,
        release_candidate_backend=lambda: released.append(True),
    )

    results = service.search("запрос", filters={"source_type": "ocr"})

    assert retriever.calls == [
        {
            "query": "запрос",
            "mode": "hybrid",
            "top_k": 3,
            "filters": {"source_type": "ocr"},
        }
    ]
    assert released == [True]
    assert reranker.calls[0][0] == "запрос"
    assert len(reranker.calls[0][1]) == 3
    assert reranker.calls[0][1][1].startswith("Section: Раздел > Подраздел 2")
    assert [result.chunk_id for result in results] == ["chunk-2", "chunk-3"]
    assert [result.reranker_score for result in results] == [pytest.approx(0.95), pytest.approx(0.6)]
    assert results[0].rrf_rank == 2 and results[0].dense_rank == 2
    assert results[0].related_image_ids == ["image-2"]
    assert results[0].provenance["ocr_provenance"][0]["ocr_source"] == "imported"
    assert results[1].provenance["source_fallback"] == "native_page_text"


def test_rerank_is_deterministic_for_equal_scores(tmp_path: Path) -> None:
    candidates = [make_result(1, text="A"), make_result(2, text="B")]
    service = RerankSearchService(
        make_settings(tmp_path),
        FakeCandidateRetriever(candidates),
        FakeReranker([0.5, 0.5]),
    )

    first = service.rerank_candidates("query", candidates, top_k=2)
    second = service.rerank_candidates("query", candidates, top_k=2)

    assert [item.chunk_id for item in first] == ["chunk-1", "chunk-2"]
    assert [item.model_dump() for item in first] == [item.model_dump() for item in second]


def test_qwen_reranker_device_resolution_supports_cpu_fallback() -> None:
    assert QwenCrossEncoderReranker.resolve_device("auto", cuda_available=True) == "cuda"
    assert QwenCrossEncoderReranker.resolve_device("auto", cuda_available=False) == "cpu"
    assert QwenCrossEncoderReranker.resolve_device("cpu", cuda_available=True) == "cpu"


@pytest.mark.parametrize(
    ("backend", "message"),
    [
        (FakeReranker(error=RuntimeError("model unavailable")), "model unavailable"),
        (FakeReranker([0.5]), "1 scores for 2 candidates"),
    ],
)
def test_reranker_failure_is_explicit(tmp_path: Path, backend: FakeReranker, message: str) -> None:
    candidates = [make_result(1, text="A"), make_result(2, text="B")]
    service = RerankSearchService(
        make_settings(tmp_path),
        FakeCandidateRetriever(candidates),
        backend,
    )

    with pytest.raises(RerankerUnavailableError, match=message):
        service.rerank_candidates("query", candidates, top_k=2)
