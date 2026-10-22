from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from corporate_rag_ingestion.context.builder import ContextBuilder
from corporate_rag_ingestion.context.repository import AssetRepository, ChunkRepository
from corporate_rag_ingestion.context.settings import ContextBuilderSettings
from corporate_rag_ingestion.retrieval.models import SearchResult


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate ContextBuilder on the real reranker baseline")
    parser.add_argument(
        "--input", type=Path, default=Path("storage/reports/reranker-baseline.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("storage/reports/context-baseline.json")
    )
    parser.add_argument("--max-context-tokens", type=int, default=6000)
    parser.add_argument("--max-chunks", type=int, default=10)
    return parser.parse_args()


def percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, round((len(ordered) - 1) * fraction))] if ordered else 0


def main() -> int:
    args = parse_args()
    input_path = args.input if args.input.is_absolute() else PROJECT_ROOT / args.input
    report = json.loads(input_path.read_text(encoding="utf-8"))
    repository = ChunkRepository(PROJECT_ROOT / "storage" / "chunks")
    context_builder = ContextBuilder(
        repository,
        AssetRepository(
            PROJECT_ROOT / "storage" / "normalized",
            PROJECT_ROOT / "База Знаний",
        ),
        settings=ContextBuilderSettings(
            max_context_tokens=args.max_context_tokens,
            max_chunks=args.max_chunks,
        ),
    )
    outputs: dict[str, object] = {}
    token_counts: list[int] = []
    source_counts: list[int] = []
    neighbor_counts: list[int] = []
    duplicate_counts: list[int] = []
    for label, item in report["queries"].items():
        results: list[SearchResult] = []
        for compact in item["final_top"]:
            chunk = repository.get(compact["chunk_id"])
            if chunk is None:
                raise RuntimeError(f"missing real chunk {compact['chunk_id']}")
            results.append(
                SearchResult(
                    final_rank=compact["rank"],
                    mode="rerank",
                    final_score=compact["reranker_score"],
                    reranker_score=compact["reranker_score"],
                    rrf_rank=compact["rrf_rank"],
                    rrf_score=compact["rrf_score"],
                    dense_rank=compact["dense_rank"],
                    dense_score=compact["dense_score"],
                    sparse_rank=compact["sparse_rank"],
                    sparse_score=compact["sparse_score"],
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    version_id=chunk.version_id,
                    relative_path=chunk.relative_path,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    heading_path=chunk.heading_path,
                    section_title=chunk.section_title,
                    text=chunk.text,
                    chunk_type=chunk.chunk_type,
                    source_type=chunk.source_type,
                    related_image_ids=chunk.related_image_ids,
                    provenance=compact["provenance"],
                    payload={},
                )
            )
        package = context_builder.build(item["query"], results)
        token_counts.append(package.token_count)
        source_counts.append(len(package.sources))
        neighbor_counts.append(package.stats.neighbors_added)
        duplicate_counts.append(
            package.stats.exact_duplicates_removed + package.stats.near_duplicates_removed
        )
        outputs[label] = {
            "query": item["query"],
            "reranker_top10": [
                {
                    "rank": result.final_rank,
                    "chunk_id": result.chunk_id,
                    "path": result.relative_path,
                    "pages": [result.page_start, result.page_end],
                }
                for result in results
            ],
            "context_summary": {
                "context_id": package.context_id,
                "token_count": package.token_count,
                "source_count": len(package.sources),
                "related_image_count": len(package.related_images),
                "stats": package.stats.model_dump(mode="json"),
            },
            "included_sources": [
                {
                    "source_number": source.source_number,
                    "citation": source.citation,
                    "chunk_id": source.chunk_id,
                    "path": source.relative_path,
                    "pages": [source.page_start, source.page_end],
                    "heading_path": source.heading_path,
                    "chunk_type": source.chunk_type,
                    "source_type": source.source_type,
                    "added_as_neighbor": source.added_as_neighbor,
                    "parent_chunk_id": source.parent_chunk_id,
                    "related_image_ids": source.related_image_ids,
                    "provenance": source.provenance,
                    "text_excerpt": source.text[:500],
                    "table": source.table.model_dump(mode="json") if source.table else None,
                }
                for source in package.sources
            ],
            "related_images": [image.model_dump(mode="json") for image in package.related_images],
        }
    summary = {
        "builder_settings": context_builder.settings.model_dump(mode="json"),
        "settings_hash": context_builder.settings.settings_hash,
        "query_count": len(outputs),
        "token_statistics": {
            "min": min(token_counts, default=0),
            "average": round(statistics.fmean(token_counts), 2) if token_counts else 0,
            "median": round(statistics.median(token_counts), 2) if token_counts else 0,
            "p95": percentile(token_counts, 0.95),
            "max": max(token_counts, default=0),
        },
        "average_reranker_results": 10,
        "average_context_sources": round(statistics.fmean(source_counts), 2),
        "total_neighbors_added": sum(neighbor_counts),
        "total_duplicates_removed": sum(duplicate_counts),
        "queries": outputs,
    }
    output_path = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "queries"}, ensure_ascii=False, indent=2))
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
