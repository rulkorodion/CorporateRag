from __future__ import annotations

"""Reproducible A/B benchmark for Ollama models over identical ContextPackage data.

The ``prepare`` phase runs retrieval/reranking exactly once and persists complete
ContextPackage JSON.  The ``run`` phase is deliberately retrieval-free: every
model receives the same package, system prompt, and generation settings.
"""

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
from typing import Any

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if importlib.util.find_spec("qdrant_client") is None:
    import torch  # noqa: F401 - keep CUDA torch from the launcher environment

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    site.addsitedir(str(PROJECT_ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from corporate_rag_ingestion.config import Settings
from corporate_rag_ingestion.context.builder import ContextBuilder
from corporate_rag_ingestion.context.models import (
    ContextBuildStats,
    ContextPackage,
    ContextSource,
    RetrievalSignals,
)
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


DEFAULT_CONTEXTS = Path("storage/reports/llm-ab-contexts.json")
DEFAULT_REPORT = Path("storage/reports/llm-ab-benchmark.json")


@dataclass(frozen=True)
class BenchmarkCase:
    label: str
    category: str
    query: str
    expected_all: tuple[str, ...] = ()
    expected_any: tuple[str, ...] = ()
    expected_source_type: str | None = None
    expected_fallback: bool = False
    expected_insufficient: bool = False
    expected_conflict: bool = False


CASES = (
    BenchmarkCase(
        "regulation",
        "ordinary_regulation",
        "Какая максимальная загрузка автомобиля?",
        expected_all=("20", "тонн"),
    ),
    BenchmarkCase(
        "exact_tu",
        "exact_tu",
        "На какие трубы распространяется ТУ 24.20.13-004-40198874-2020?",
        expected_all=("15", "100", "труб"),
    ),
    BenchmarkCase(
        "table",
        "table",
        "Кто подаёт заявление на открытие отдельного счёта по ГОЗ и кому направляют уведомление?",
        expected_all=("Казначейство", "менеджер"),
    ),
    BenchmarkCase(
        "ocr",
        "ocr",
        "Какая допустимая местная кривизна профиля на один метр длины?",
        expected_all=("1,5", "мм"),
        expected_source_type="ocr",
    ),
    BenchmarkCase(
        "fallback",
        "fallback_native_text",
        "Как маленькие ежедневные упражнения помогают сформировать лидерскую привычку?",
        expected_any=("привыч", "пятиминут"),
        expected_fallback=True,
    ),
    BenchmarkCase(
        "outlook",
        "outlook_with_images",
        "Как создать и настроить корпоративную подпись в Outlook?",
        expected_any=("Подпис", "подпис"),
    ),
    BenchmarkCase(
        "business_card",
        "real_business_card_contact",
        "К кому обращаться, чтобы заказать визитку?",
        expected_all=("Бенько", "Reception"),
    ),
    BenchmarkCase(
        "semantic_delivery",
        "semantic_paraphrase",
        "Сколько металла разрешено положить в машину, чтобы не получить штраф за перегруз?",
        expected_all=("20", "тонн"),
    ),
    BenchmarkCase(
        "semantic_leadership",
        "semantic_paraphrase",
        "Почему лидерские навыки лучше тренировать понемногу каждый день, а не только изучать в теории?",
        expected_any=("привыч", "ежеднев", "практи"),
        expected_fallback=True,
    ),
    BenchmarkCase(
        "no_answer",
        "insufficient_context",
        "Какой пароль установлен на Wi-Fi в переговорной директора?",
        expected_insufficient=True,
    ),
)


def _path(value: Path) -> Path:
    return value if value.is_absolute() else PROJECT_ROOT / value


def _controlled_packages() -> dict[str, ContextPackage]:
    empty = ContextPackage(
        query="Какой пароль установлен на Wi-Fi в переговорной директора?",
        context_id="controlled-empty-context-v1",
        builder_version="controlled-benchmark-v1",
        settings_hash="controlled-empty-v1",
        token_counter="controlled",
        token_count=0,
        rendered_context="",
        sources=[],
        related_images=[],
        stats=ContextBuildStats(),
    )
    conflict_sources = [
        ContextSource(
            source_number=1,
            citation="[1]",
            chunk_id="controlled-conflict-a",
            document_id="controlled-document-a",
            version_id="controlled-version-a",
            relative_path="Контрольный источник A.txt",
            text="Максимальная допустимая загрузка автомобиля составляет 20 тонн.",
            chunk_type="text",
            retrieval=RetrievalSignals(reranker_rank=1),
            source_type="native",
        ),
        ContextSource(
            source_number=2,
            citation="[2]",
            chunk_id="controlled-conflict-b",
            document_id="controlled-document-b",
            version_id="controlled-version-b",
            relative_path="Контрольный источник B.txt",
            text="Максимальная допустимая загрузка автомобиля составляет 18 тонн.",
            chunk_type="text",
            retrieval=RetrievalSignals(reranker_rank=2),
            source_type="native",
        ),
    ]
    rendered = "\n\n".join(f"{item.citation} {item.text}" for item in conflict_sources)
    conflict = ContextPackage(
        query="Какова максимальная допустимая загрузка автомобиля?",
        context_id="controlled-conflict-context-v1",
        builder_version="controlled-benchmark-v1",
        settings_hash="controlled-conflict-v1",
        token_counter="controlled",
        token_count=32,
        rendered_context=rendered,
        sources=conflict_sources,
        related_images=[],
        stats=ContextBuildStats(input_results=2, primary_sources=2),
    )
    missing_field_source = ContextSource(
        source_number=1,
        citation="[1]",
        chunk_id="controlled-missing-business-card-contact",
        document_id="controlled-business-card-document",
        version_id="controlled-business-card-version",
        relative_path="Контрольная таблица контактов.xlsx",
        text=(
            'Table: {"headers":["Раздел","Тема обращения","К кому обращаться",'
            '"Способ связи"],"rows":[["Канцелярия","Заказать визитки","",""]]}'
        ),
        chunk_type="table",
        retrieval=RetrievalSignals(reranker_rank=1),
        source_type="native",
    )
    missing_field = ContextPackage(
        query="К кому обращаться, чтобы заказать визитку?",
        context_id="controlled-missing-field-context-v1",
        builder_version="controlled-benchmark-v1",
        settings_hash="controlled-missing-field-v1",
        token_counter="controlled",
        token_count=32,
        rendered_context=f"[1] {missing_field_source.text}",
        sources=[missing_field_source],
        related_images=[],
        stats=ContextBuildStats(input_results=1, primary_sources=1),
    )
    return {
        "no_answer": empty,
        "controlled_conflict": conflict,
        "controlled_missing_field": missing_field,
    }


def prepare_contexts(output: Path, candidates: int, top_k: int) -> None:
    project = Settings()
    retrieval = RetrievalSettings().resolve_paths(PROJECT_ROOT)
    store = QdrantStore(retrieval)
    dense = QwenDenseEmbedder(retrieval)
    sparse = BM25SparseEmbedder(retrieval)
    hybrid = HybridSearchService(retrieval, store, dense, sparse)
    candidate_sets: dict[str, Any] = {}
    packages: dict[str, ContextPackage] = {}
    try:
        for case in CASES:
            if case.label == "no_answer":
                continue
            candidate_sets[case.label] = hybrid.search(
                case.query, mode="hybrid", top_k=candidates
            )
        dense.release()
        reranker = QwenCrossEncoderReranker(retrieval)
        service = RerankSearchService(retrieval, hybrid, reranker)
        builder = ContextBuilder(
            ChunkRepository(project.chunk_storage_root),
            AssetRepository(project.normalized_root, project.knowledge_base_root),
            settings=ContextBuilderSettings(max_context_tokens=6000, max_chunks=10),
        )
        try:
            for case in CASES:
                if case.label == "no_answer":
                    continue
                reranked = service.rerank_candidates(
                    case.query, candidate_sets[case.label], top_k=top_k
                )
                packages[case.label] = builder.build(case.query, reranked)
        finally:
            reranker.release()
    finally:
        store.close()
    packages.update(_controlled_packages())
    payload = {
        "schema_version": "1.0",
        "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "retrieval_candidates": candidates,
        "reranker_top_k": top_k,
        "cases": {
            case.label: {
                "case": case.__dict__,
                "controlled": False,
                "context": packages[case.label].model_dump(mode="json"),
            }
            for case in CASES
        },
    }
    conflict_case = BenchmarkCase(
        "controlled_conflict",
        "controlled_conflicting_sources",
        "Какова максимальная допустимая загрузка автомобиля?",
        expected_all=("20", "18"),
        expected_conflict=True,
    )
    payload["cases"][conflict_case.label] = {
        "case": conflict_case.__dict__,
        "controlled": True,
        "context": packages[conflict_case.label].model_dump(mode="json"),
    }
    missing_field_case = BenchmarkCase(
        "controlled_missing_field",
        "controlled_empty_table_field",
        "К кому обращаться, чтобы заказать визитку?",
        expected_insufficient=True,
    )
    payload["cases"][missing_field_case.label] = {
        "case": missing_field_case.__dict__,
        "controlled": True,
        "context": packages[missing_field_case.label].model_dump(mode="json"),
    }
    payload["cases"]["no_answer"]["controlled"] = True
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"contexts": len(payload["cases"]), "output": str(output)}, ensure_ascii=False))


def _runtime_model(base_url: str, model: str) -> dict[str, Any] | None:
    with httpx.Client(base_url=base_url, timeout=15, trust_env=False) as client:
        response = client.get("/api/ps")
        response.raise_for_status()
        for item in response.json().get("models", []):
            if item.get("name") == model or item.get("model") == model:
                return item
    return None


def _automatic_checks(case: dict[str, Any], result: dict[str, Any]) -> dict[str, bool]:
    answer = result["answer"].casefold()
    all_terms = [term.casefold() for term in case.get("expected_all", [])]
    any_terms = [term.casefold() for term in case.get("expected_any", [])]
    insufficient_markers = ("недостаточ", "не указан", "нет информац", "не содержат")
    conflict_markers = (
        "противореч",
        "расхожд",
        "разные значения",
        "в разных источниках",
        "в одном",
    )
    return {
        "citation_validation": result["citation_validation"] == "valid",
        "expected_all_terms": all(term in answer for term in all_terms),
        "expected_any_term": not any_terms or any(term in answer for term in any_terms),
        "insufficient_behavior": (
            not case.get("expected_insufficient")
            or any(marker in answer for marker in insufficient_markers)
        ),
        "conflict_behavior": (
            not case.get("expected_conflict")
            or any(marker in answer for marker in conflict_markers)
        ),
    }


def run_benchmark(
    contexts_file: Path,
    output: Path,
    models: list[str],
    base_url: str,
    num_ctx: int,
) -> None:
    prepared = json.loads(contexts_file.read_text(encoding="utf-8"))
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "contexts_file": str(contexts_file),
        "prepared_at": prepared["prepared_at"],
        "benchmark_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "shared_settings": {
            "base_url": base_url,
            "temperature": 0.1,
            "num_ctx": num_ctx,
            "keep_alive": "60m",
            "think": False,
            "stream": False,
            "timeout_seconds": 120,
        },
        "models": {},
    }
    for model in models:
        settings = GenerationSettings(
            base_url=base_url,
            model=model,
            temperature=0.1,
            num_ctx=num_ctx,
            keep_alive="60m",
            think=False,
            stream=False,
            timeout_seconds=120,
        )
        model_cases: dict[str, Any] = {}
        latencies: list[float] = []
        ollama_latencies: list[float] = []
        with OllamaClient(settings) as client:
            generator = RAGAnswerService(client, settings)
            for label, item in prepared["cases"].items():
                context = ContextPackage.model_validate(item["context"])
                answer = generator.generate(context)
                dumped = answer.model_dump(mode="json")
                dumped["automatic_checks"] = _automatic_checks(item["case"], dumped)
                dumped["controlled_context"] = bool(item.get("controlled"))
                model_cases[label] = dumped
                latencies.append(answer.latency.llm_seconds)
                if answer.latency.ollama_total_seconds is not None:
                    ollama_latencies.append(answer.latency.ollama_total_seconds)
                print(
                    json.dumps(
                        {
                            "model": model,
                            "case": label,
                            "seconds": answer.latency.llm_seconds,
                            "citations": answer.citation_validation,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        checks = [check for item in model_cases.values() for check in item["automatic_checks"].values()]
        report["models"][model] = {
            "settings_hash": settings.settings_hash,
            "case_count": len(model_cases),
            "automatic_checks_passed": sum(checks),
            "automatic_checks_total": len(checks),
            "invalid_citation_answers": sum(
                item["citation_validation"] == "invalid" for item in model_cases.values()
            ),
            "warning_citation_answers": sum(
                item["citation_validation"] == "warning" for item in model_cases.values()
            ),
            "average_llm_seconds": round(statistics.fmean(latencies), 4),
            "median_llm_seconds": round(statistics.median(latencies), 4),
            "average_ollama_seconds": (
                round(statistics.fmean(ollama_latencies), 4) if ollama_latencies else None
            ),
            "runtime": _runtime_model(base_url, model),
            "cases": model_cases,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"models": models, "output": str(output)}, ensure_ascii=False))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--output", type=Path, default=DEFAULT_CONTEXTS)
    prepare.add_argument("--candidates", type=int, default=40)
    prepare.add_argument("--top-k", type=int, default=10)
    run = subparsers.add_parser("run")
    run.add_argument("--contexts", type=Path, default=DEFAULT_CONTEXTS)
    run.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    run.add_argument("--models", nargs="+", default=["qwen3.6:latest", "qwen3:8b"])
    run.add_argument("--base-url", default="http://127.0.0.1:11435")
    run.add_argument("--num-ctx", type=int, default=12288)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "prepare":
        prepare_contexts(_path(args.output), args.candidates, args.top_k)
    else:
        run_benchmark(
            _path(args.contexts),
            _path(args.output),
            args.models,
            args.base_url,
            args.num_ctx,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
