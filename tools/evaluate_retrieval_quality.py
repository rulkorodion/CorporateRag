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

from corporate_rag_ingestion.retrieval.dense import QwenDenseEmbedder
from corporate_rag_ingestion.retrieval.models import SearchResult
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
    expected_paths: tuple[str, ...]
    expected_text: tuple[str, ...] = ()


CASES = (
    Case("biosmart_exact", "Сканирование ладони на сканере Biosmart", ("Сканирование ладони на сканере Biosmart.png",)),
    Case("biosmart_short", "сканирование ладони", ("Сканирование ладони на сканере Biosmart.png",)),
    Case("business_cards", "к кому обратиться, чтобы заказать визитку?", ("К кому по какому вопросу обратиться",), ("Бенько Мария Александровна",)),
    Case("business_cards_contact", "как связаться с человеком, который заказывает визитки и канцелярию?", ("К кому по какому вопросу обратиться",), ("info@ktzholding.ru",)),
    Case("max_vehicle_load", "какая максимальная загрузка автомобиля?", ("Доставки/Регламент.pdf",), ("20",)),
    Case("overload_contact", "с кем согласовать перегруз двадцатитонника на три тонны?", ("К кому по какому вопросу обратиться",), ("Турманова",)),
    Case("urgent_delivery", "к кому обращаться по срочной доставке вне регламента?", ("К кому по какому вопросу обратиться",), ("Бутенко",)),
    Case("corporate_taxi", "как подключить корпоративное такси?", ("К кому по какому вопросу обратиться",), ("Бенько",)),
    Case("outlook", "как настроить подпись в Outlook?", ("Outlook",)),
    Case("mobile_outlook", "как подключить подпись в мобильном Outlook?", ("Outlook",)),
    Case("state_defense", "кто ответственный за открытие специального счета по ГОЗ?", ("Гособоронзаказ",)),
    Case("tu_exact", "ТУ 24.20.13-004-40198874-2020", ("ТУ 24.20.13-004-40198874-2020",)),
    Case("ocr_profile", "какая допустимая местная кривизна профиля на один метр длины?", ("Католожный лист ТУ 24.20.34-002-33316304-2020.pdf",)),
    Case("fallback_book", "почему лидерство нужно тренировать до автоматической привычки?", ("Книги/Привычки лидера.pdf",)),
    Case("truck_products", "какие трубы мы производим?", ("Наше дело труба",)),
    Case("reserve", "к кому обращаться, чтобы подсадить трубу в оплаченный счет из запасов?", ("К кому по какому вопросу обратиться",), ("Кузнецова",)),
    Case("it_access", "куда писать, если нет доступа к папке на сетевом диске?", ("К кому по какому вопросу обратиться",), ("it@ktzholding.ru",)),
    Case("hotel", "кто бронирует гостиницу для командировки?", ("К кому по какому вопросу обратиться",), ("Бенько",)),
    Case("original_letter", "куда передать оригинал письма от клиента?", ("К кому по какому вопросу обратиться",)),
    Case("counterparty_visit", "с кем согласовать выезд к клиенту для проверки контрагента?", ("К кому по какому вопросу обратиться",), ("Каретин",)),
)


def matches(result: SearchResult, case: Case) -> bool:
    path_ok = any(value.casefold() in result.relative_path.casefold() for value in case.expected_paths)
    text_ok = not case.expected_text or any(
        value.casefold() in result.text.casefold() for value in case.expected_text
    )
    return path_ok and text_ok


def rank(results: list[SearchResult], case: Case) -> int | None:
    return next((index for index, result in enumerate(results, start=1) if matches(result, case)), None)


def compact(result: SearchResult) -> dict[str, object]:
    return {
        "rank": result.final_rank,
        "path": result.relative_path,
        "chunk_id": result.chunk_id,
        "score": result.final_score,
        "text": result.text[:450],
    }


def metrics(ranks: list[int | None]) -> dict[str, float]:
    return {
        "hit_at_1": sum(item == 1 for item in ranks) / len(ranks),
        "hit_at_3": sum(item is not None and item <= 3 for item in ranks) / len(ranks),
        "hit_at_5": sum(item is not None and item <= 5 for item in ranks) / len(ranks),
        "hit_at_10": sum(item is not None and item <= 10 for item in ranks) / len(ranks),
        "mrr": sum(1 / item for item in ranks if item is not None) / len(ranks),
    }


def main() -> int:
    settings = RetrievalSettings().resolve_paths(PROJECT_ROOT)
    store = QdrantStore(settings)
    dense = QwenDenseEmbedder(settings)
    sparse = BM25SparseEmbedder(settings)
    hybrid = HybridSearchService(settings, store, dense, sparse)
    before: dict[str, list[SearchResult]] = {}
    retrieval_times = []
    try:
        for case in CASES:
            started = time.perf_counter()
            before[case.label] = hybrid.search(case.query, mode="hybrid", top_k=40)
            retrieval_times.append(time.perf_counter() - started)
        dense.release()
        reranker = QwenCrossEncoderReranker(settings)
        service = RerankSearchService(settings, hybrid, reranker)
        rows = {}
        before_ranks = []
        after_ranks = []
        rerank_times = []
        for case in CASES:
            started = time.perf_counter()
            after = service.rerank_candidates(case.query, before[case.label], top_k=10)
            rerank_times.append(time.perf_counter() - started)
            before_rank = rank(before[case.label], case)
            after_rank = rank(after, case)
            before_ranks.append(before_rank)
            after_ranks.append(after_rank)
            rows[case.label] = {
                "query": case.query,
                "expected_paths": case.expected_paths,
                "expected_text": case.expected_text,
                "hybrid_rank": before_rank,
                "rerank_rank": after_rank,
                "top5": [compact(result) for result in after[:5]],
            }
        report = {
            "case_count": len(CASES),
            "metrics_hybrid": metrics(before_ranks),
            "metrics_rerank": metrics(after_ranks),
            "average_retrieval_seconds": statistics.fmean(retrieval_times),
            "average_rerank_seconds": statistics.fmean(rerank_times),
            "cases": rows,
        }
    finally:
        store.close()
    output = PROJECT_ROOT / "storage" / "reports" / "retrieval-quality-v1.2.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
