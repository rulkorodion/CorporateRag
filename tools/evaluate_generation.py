from __future__ import annotations

import argparse
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
    import torch  # noqa: F401 - preserve CUDA torch from the launcher environment

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    site.addsitedir(str(PROJECT_ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

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
class QueryCase:
    label: str
    query: str


CASES = (
    QueryCase("regulation", "какая максимальная загрузка автомобиля?"),
    QueryCase("exact_tu", "какие требования установлены ТУ 24.20.13-004-40198874-2020?"),
    QueryCase("table", "кто ответственен за открытие специального счета по ГОЗ?"),
    QueryCase(
        "ocr",
        "какая допустимая местная кривизна профиля на один метр длины?",
    ),
    QueryCase("fallback", "как сформировать устойчивые лидерские привычки?"),
    QueryCase("outlook_images", "как настроить подпись в Outlook?"),
    QueryCase("insufficient_business_card", "к кому обращаться, чтобы заказать визитку?"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the real Stage 7 RAG evaluation")
    parser.add_argument("--candidates", type=int, default=40)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--output", type=Path, default=Path("storage/reports/generation-smoke.json")
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    retrieval_settings = RetrievalSettings().resolve_paths(PROJECT_ROOT)
    generation_settings = GenerationSettings()
    store = QdrantStore(retrieval_settings)
    dense = QwenDenseEmbedder(retrieval_settings)
    sparse = BM25SparseEmbedder(retrieval_settings)
    hybrid = HybridSearchService(retrieval_settings, store, dense, sparse)
    candidates_by_label = {}
    retrieval_seconds: dict[str, float] = {}
    report_cases: dict[str, object] = {}
    try:
        for case in CASES:
            started = time.perf_counter()
            candidates_by_label[case.label] = hybrid.search(
                case.query, mode="hybrid", top_k=args.candidates
            )
            retrieval_seconds[case.label] = time.perf_counter() - started
        dense.release()

        reranker = QwenCrossEncoderReranker(retrieval_settings)
        rerank_service = RerankSearchService(retrieval_settings, hybrid, reranker)
        context_builder = ContextBuilder(
            ChunkRepository(PROJECT_ROOT / "storage" / "chunks"),
            AssetRepository(
                PROJECT_ROOT / "storage" / "normalized",
                PROJECT_ROOT / "База Знаний",
            ),
            settings=ContextBuilderSettings(),
        )
        with OllamaClient(generation_settings) as ollama:
            generator = RAGAnswerService(ollama, generation_settings)
            totals: list[float] = []
            llm_times: list[float] = []
            invalid_count = 0
            insufficient_count = 0
            for case in CASES:
                rerank_started = time.perf_counter()
                reranked = rerank_service.rerank_candidates(
                    case.query,
                    candidates_by_label[case.label],
                    top_k=args.top_k,
                )
                rerank_seconds = time.perf_counter() - rerank_started
                context_started = time.perf_counter()
                context = context_builder.build(case.query, reranked)
                context_seconds = time.perf_counter() - context_started
                upstream = retrieval_seconds[case.label] + rerank_seconds + context_seconds
                answer = generator.generate(
                    context, retrieval_context_seconds=upstream
                )
                totals.append(answer.latency.total_seconds)
                llm_times.append(answer.latency.llm_seconds)
                invalid_count += answer.citation_validation == "invalid"
                insufficient = "недостат" in answer.answer.casefold() or "не указан" in answer.answer.casefold()
                insufficient_count += insufficient
                report_cases[case.label] = {
                    "query": case.query,
                    "answer": answer.answer,
                    "status": answer.status,
                    "citation_validation": answer.citation_validation,
                    "citations": [item.model_dump(mode="json") for item in answer.citations],
                    "used_sources": [
                        item.model_dump(mode="json") for item in answer.used_sources
                    ],
                    "related_images": [
                        item.model_dump(mode="json") for item in answer.related_images
                    ],
                    "warnings": answer.warnings,
                    "latency": answer.latency.model_dump(mode="json"),
                    "context": {
                        "context_id": context.context_id,
                        "token_count": context.token_count,
                        "source_count": len(context.sources),
                        "related_image_count": len(context.related_images),
                    },
                    "insufficient_context_answer": insufficient,
                }
            report = {
                "query_count": len(CASES),
                "model": generation_settings.model,
                "generation_settings": generation_settings.model_dump(
                    mode="json", exclude={"base_url"}
                ),
                "generation_settings_hash": generation_settings.settings_hash,
                "retrieval_candidates": args.candidates,
                "reranker_top_k": args.top_k,
                "average_full_pipeline_seconds": round(statistics.fmean(totals), 4),
                "average_llm_seconds": round(statistics.fmean(llm_times), 4),
                "invalid_citation_answers": invalid_count,
                "insufficient_context_answers": insufficient_count,
                "reranker_runtime": reranker.runtime.model_dump(mode="json"),
                "cases": report_cases,
            }
    finally:
        store.close()

    output = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key not in {"cases", "reranker_runtime"}},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(output)
    return 1 if report["invalid_citation_answers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
