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
    import torch  # noqa: F401 - preserve the launch environment's CUDA build

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    site.addsitedir(str(PROJECT_ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from corporate_rag_ingestion.retrieval.dense import QwenDenseEmbedder
from corporate_rag_ingestion.retrieval.models import SearchResult
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
    expected_path_fragments: tuple[str, ...]
    metric_eligible: bool = True


CASES = (
    QueryCase("semantic_question", "какая максимальная загрузка автомобиля?", ("Доставки/Регламент.pdf",)),
    QueryCase("exact_standard", "ТУ 24.20.13-004-40198874-2020", ("ТУ 24.20.13-004-40198874-2020 ВГП ТЗ/",)),
    QueryCase("numeric_limit", "максимальная загрузка машины 20 тонн", ("Доставки/Регламент.pdf",)),
    QueryCase(
        "regulation_term",
        "порядок претензионной работы",
        ("Текстовый 13.08 ПРЕЗЕНТАЦИОННАЯ РАБОТА дополнения.docx",),
        metric_eligible=False,
    ),
    QueryCase("ocr_document", "какая допустимая местная кривизна профиля на один метр длины?", ("Католожный лист ТУ 24.20.34-002-33316304-2020.pdf",)),
    QueryCase("table_question", "кто ответственный за открытие специального счета по ГОЗ?", ("Гособоронзаказ (275 ФЗ)_регламент (2026)_v3.xlsx",)),
    QueryCase("fallback_book", "как сформировать устойчивые лидерские привычки", ("Книги/Привычки лидера.pdf",)),
    QueryCase("related_image", "как настроить подпись в Outlook", ("Как настроить подпись в Outlook.pdf", "Инструкция по настройке подписи в Outlook.pdf")),
    QueryCase("similar_tu", "требования к водогазопроводной трубе по ТУ 24.20.13-004-40198874-2020", ("ТУ 24.20.13-004-40198874-2020 ВГП ТЗ/",)),
    QueryCase("table_row", "кто подает заявление на открытие отдельного счета по ГОЗ и кому направляется информация?", ("Гособоронзаказ (275 ФЗ)_регламент (2026)_v3.xlsx",)),
    QueryCase("semantic_paraphrase", "сколько металла разрешено положить в машину, чтобы не получить штраф?", ("Доставки/Регламент.pdf",)),
    QueryCase("fallback_paraphrase", "почему лидерство нужно тренировать до автоматической привычки?", ("Книги/Привычки лидера.pdf",)),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate hybrid retrieval before/after reranking")
    parser.add_argument("--candidates", type=int, default=40)
    parser.add_argument("--final-top-k", type=int, default=10)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("storage/reports/reranker-baseline.json"),
    )
    return parser.parse_args()


def is_expected(result: SearchResult, case: QueryCase) -> bool:
    return any(fragment in result.relative_path for fragment in case.expected_path_fragments)


def expected_rank(results: list[SearchResult], case: QueryCase) -> int | None:
    return next((index for index, result in enumerate(results, start=1) if is_expected(result, case)), None)


def compact(result: SearchResult) -> dict[str, object]:
    return {
        "rank": result.final_rank,
        "reranker_score": result.reranker_score,
        "rrf_rank": result.rrf_rank,
        "rrf_score": result.rrf_score,
        "dense_rank": result.dense_rank,
        "dense_score": result.dense_score,
        "sparse_rank": result.sparse_rank,
        "sparse_score": result.sparse_score,
        "chunk_id": result.chunk_id,
        "path": result.relative_path,
        "pages": [result.page_start, result.page_end],
        "heading": result.heading_path,
        "source_type": result.source_type,
        "related_image_ids": result.related_image_ids,
        "provenance": result.provenance,
        "text_excerpt": result.text[:500],
    }


def metric_summary(ranks: list[int | None]) -> dict[str, float]:
    total = max(1, len(ranks))
    return {
        "hit_at_1": sum(rank is not None and rank <= 1 for rank in ranks) / total,
        "hit_at_3": sum(rank is not None and rank <= 3 for rank in ranks) / total,
        "hit_at_5": sum(rank is not None and rank <= 5 for rank in ranks) / total,
        "mrr": sum(1 / rank for rank in ranks if rank is not None) / total,
    }


def main() -> int:
    args = parse_args()
    settings = RetrievalSettings().resolve_paths(PROJECT_ROOT)
    store = QdrantStore(settings)
    dense = QwenDenseEmbedder(settings)
    sparse = BM25SparseEmbedder(settings)
    hybrid = HybridSearchService(settings, store, dense, sparse)
    candidates_by_label: dict[str, list[SearchResult]] = {}
    retrieve_seconds: dict[str, float] = {}
    try:
        # Phase 1: retrieve every query while the embedding model is resident.
        for case in CASES:
            started = time.perf_counter()
            candidates_by_label[case.label] = hybrid.search(
                case.query,
                mode="hybrid",
                top_k=args.candidates,
            )
            retrieve_seconds[case.label] = time.perf_counter() - started
        dense.release()

        # Phase 2: only the reranker is resident on the GPU.
        reranker = QwenCrossEncoderReranker(settings)
        rerank_service = RerankSearchService(settings, hybrid, reranker)
        queries: dict[str, object] = {}
        before_ranks: list[int | None] = []
        after_ranks: list[int | None] = []
        rerank_times: list[float] = []
        for case in CASES:
            candidates = candidates_by_label[case.label]
            started = time.perf_counter()
            reranked_all = rerank_service.rerank_candidates(
                case.query,
                candidates,
                top_k=len(candidates),
            )
            rerank_seconds = time.perf_counter() - started
            rerank_times.append(rerank_seconds)
            before_rank = expected_rank(candidates, case)
            after_rank = expected_rank(reranked_all, case)
            if case.metric_eligible:
                before_ranks.append(before_rank)
                after_ranks.append(after_rank)
            queries[case.label] = {
                "query": case.query,
                "expected_path_fragments": case.expected_path_fragments,
                "metric_eligible": case.metric_eligible,
                "candidate_count": len(candidates),
                "retrieve_seconds": round(retrieve_seconds[case.label], 4),
                "rerank_seconds": round(rerank_seconds, 4),
                "expected_rank_before": before_rank,
                "expected_rank_after": after_rank,
                "top5_before": [compact(result) for result in candidates[:5]],
                "top5_after": [compact(result) for result in reranked_all[:5]],
                "final_top": [compact(result) for result in reranked_all[: args.final_top_k]],
            }
        report = {
            "evaluation_basis": (
                "Only explicit expected document paths from confirmed Stage 4 examples; "
                "no unjudged relevance labels are inferred"
            ),
            "candidate_limit": args.candidates,
            "final_top_k": args.final_top_k,
            "query_count": len(CASES),
            "metric_query_count": len(before_ranks),
            "reranker_runtime": reranker.runtime.model_dump(mode="json"),
            "timing": {
                "average_rerank_seconds": round(statistics.fmean(rerank_times), 4),
                "median_rerank_seconds": round(statistics.median(rerank_times), 4),
                "cold_rerank_seconds": round(rerank_times[0], 4),
                "warm_average_rerank_seconds": round(statistics.fmean(rerank_times[1:]), 4),
            },
            "metrics_before": metric_summary(before_ranks),
            "metrics_after": metric_summary(after_ranks),
            "queries": queries,
        }
    finally:
        store.close()

    output = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
