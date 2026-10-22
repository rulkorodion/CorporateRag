from __future__ import annotations

import importlib.util
import json
import os
import site
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if importlib.util.find_spec("qdrant_client") is None:
    import torch  # noqa: F401

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    site.addsitedir(str(PROJECT_ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from corporate_rag_ingestion.config import Settings
from corporate_rag_ingestion.context.builder import ContextBuilder
from corporate_rag_ingestion.context.repository import AssetRepository, ChunkRepository
from corporate_rag_ingestion.context.settings import ContextBuilderSettings
from corporate_rag_ingestion.generation.ollama import OllamaClient
from corporate_rag_ingestion.generation.service import RAGAnswerService
from corporate_rag_ingestion.generation.settings import GenerationSettings
from corporate_rag_ingestion.retrieval.dense import QwenDenseEmbedder
from corporate_rag_ingestion.retrieval.rerank_service import RerankSearchService
from corporate_rag_ingestion.retrieval.reranker import QwenCrossEncoderReranker
from corporate_rag_ingestion.retrieval.search import HybridSearchService
from corporate_rag_ingestion.retrieval.settings import RetrievalSettings
from corporate_rag_ingestion.retrieval.sparse import BM25SparseEmbedder
from corporate_rag_ingestion.retrieval.store import QdrantStore


@dataclass(frozen=True)
class Case:
    label: str
    query: str
    expected_path: str
    expected_answer_terms: tuple[str, ...]
    expect_image: bool = False


CASES = (
    Case("biosmart_exact", "Сканирование ладони на сканере Biosmart", "Сканирование ладони на сканере Biosmart.png", ("файл",), True),
    Case("biosmart_short", "сканирование ладони", "Сканирование ладони на сканере Biosmart.png", ("файл",), True),
    Case("business_cards", "к кому обратиться, чтобы заказать визитку?", "К кому по какому вопросу обратиться", ("Бенько", "Reception")),
    Case("business_cards_contact", "как связаться с человеком, который заказывает визитки и канцелярию?", "К кому по какому вопросу обратиться", ("info@ktzholding.ru", "4004")),
    Case("urgent_delivery", "к кому обращаться по срочной доставке вне регламента?", "К кому по какому вопросу обратиться", ("Бутенко",)),
    Case("it_access", "куда писать, если нет доступа к папке на сетевом диске?", "К кому по какому вопросу обратиться", ("it@ktzholding.ru",)),
    Case("hotel", "кто бронирует гостиницу для командировки?", "К кому по какому вопросу обратиться", ("Бенько",)),
    Case("outlook", "как настроить подпись в Outlook?", "Outlook", ("подпис",), True),
)


def main() -> int:
    project = Settings()
    retrieval = RetrievalSettings().resolve_paths(PROJECT_ROOT)
    store = QdrantStore(retrieval)
    dense = QwenDenseEmbedder(retrieval)
    sparse = BM25SparseEmbedder(retrieval)
    hybrid = HybridSearchService(retrieval, store, dense, sparse)
    contexts = {}
    retrieval_times = {}
    try:
        for case in CASES:
            started = time.perf_counter()
            contexts[case.label] = hybrid.search(case.query, mode="hybrid", top_k=40)
            retrieval_times[case.label] = time.perf_counter() - started
        dense.release()
        reranker = QwenCrossEncoderReranker(retrieval)
        service = RerankSearchService(retrieval, hybrid, reranker)
        builder = ContextBuilder(
            ChunkRepository(project.chunk_storage_root),
            AssetRepository(project.normalized_root, project.knowledge_base_root),
            settings=ContextBuilderSettings(max_context_tokens=6000, max_chunks=8),
        )
        generation = GenerationSettings()
        ollama = OllamaClient(generation)
        generator = RAGAnswerService(ollama, generation)
        rows = {}
        latencies = []
        try:
            for case in CASES:
                reranked = service.rerank_candidates(case.query, contexts[case.label], top_k=8)
                context = builder.build(case.query, reranked)
                answer = generator.generate(context, retrieval_context_seconds=retrieval_times[case.label])
                latencies.append(answer.latency.total_seconds)
                path_ok = any(case.expected_path.casefold() in source.relative_path.casefold() for source in answer.used_sources)
                terms_ok = all(term.casefold() in answer.answer.casefold() for term in case.expected_answer_terms)
                image_ok = bool(answer.related_images) if case.expect_image else True
                rows[case.label] = {
                    "query": case.query,
                    "answer": answer.answer,
                    "status": answer.status,
                    "citation_validation": answer.citation_validation,
                    "path_ok": path_ok,
                    "terms_ok": terms_ok,
                    "image_ok": image_ok,
                    "used_sources": [source.relative_path for source in answer.used_sources],
                    "related_images": [image.local_path for image in answer.related_images],
                    "latency": answer.latency.model_dump(mode="json"),
                    "warnings": answer.warnings,
                }
        finally:
            ollama.close()
        report = {
            "case_count": len(CASES),
            "passed": sum(
                row["citation_validation"] == "valid"
                and row["path_ok"]
                and row["terms_ok"]
                and row["image_ok"]
                for row in rows.values()
            ),
            "average_total_seconds": statistics.fmean(latencies),
            "cases": rows,
        }
    finally:
        store.close()
    output = PROJECT_ROOT / "storage" / "reports" / "answer-quality-v1.2.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
