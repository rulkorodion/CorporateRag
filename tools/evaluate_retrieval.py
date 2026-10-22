from __future__ import annotations

import argparse
import importlib.util
import json
import os
import site
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if importlib.util.find_spec("qdrant_client") is None:
    # Support execution from the existing CUDA environment without modifying it.
    import torch  # noqa: F401 - locks in the launch environment's CUDA build

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    site.addsitedir(str(PROJECT_ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from corporate_rag_ingestion.retrieval.dense import QwenDenseEmbedder
from corporate_rag_ingestion.retrieval.search import HybridSearchService
from corporate_rag_ingestion.retrieval.settings import RetrievalSettings
from corporate_rag_ingestion.retrieval.sparse import BM25SparseEmbedder
from corporate_rag_ingestion.retrieval.store import QdrantStore


DEFAULT_QUERIES = {
    "semantic_question": "какая максимальная загрузка автомобиля?",
    "exact_standard": "ТУ 24.20.13-004-40198874-2020",
    "numeric_limit": "максимальная загрузка машины 20 тонн",
    "regulation_term": "порядок претензионной работы",
    "ocr_document": "какая допустимая местная кривизна профиля на один метр длины?",
    "table_question": "кто ответственный за открытие специального счета по ГОЗ?",
    "fallback_book": "как сформировать устойчивые лидерские привычки",
    "related_image": "как настроить подпись в Outlook",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the reproducible Stage 4 retrieval baseline")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("storage/reports/retrieval-baseline.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = PROJECT_ROOT
    settings = RetrievalSettings().resolve_paths(project_root)
    store = QdrantStore(settings)
    service = HybridSearchService(
        settings,
        store,
        QwenDenseEmbedder(settings),
        BM25SparseEmbedder(settings),
    )
    report: dict[str, object] = {
        "collection": settings.collection_name,
        "top_k": args.top_k,
        "queries": {},
    }
    try:
        queries: dict[str, object] = {}
        for label, query in DEFAULT_QUERIES.items():
            by_mode: dict[str, object] = {}
            for mode in ("dense", "sparse", "hybrid"):
                matches = service.search(query, mode=mode, top_k=args.top_k)
                by_mode[mode] = [
                    {
                        "rank": item.final_rank,
                        "chunk_id": item.chunk_id,
                        "path": item.relative_path,
                        "pages": [item.page_start, item.page_end],
                        "heading": item.heading_path,
                        "dense_rank": item.dense_rank,
                        "dense_score": item.dense_score,
                        "sparse_rank": item.sparse_rank,
                        "sparse_score": item.sparse_score,
                        "rrf_score": item.rrf_score,
                        "related_image_ids": item.related_image_ids,
                        "text_excerpt": item.text[:500],
                    }
                    for item in matches
                ]
            queries[label] = {"query": query, "modes": by_mode}
        report["queries"] = queries
    finally:
        store.close()

    output = args.output if args.output.is_absolute() else project_root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
