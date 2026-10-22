from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from corporate_rag_ingestion.config import Settings
from corporate_rag_ingestion.context.builder import ContextBuilder
from corporate_rag_ingestion.context.models import (
    ContextBuildStats,
    ContextPackage,
    TableRepresentation,
)
from corporate_rag_ingestion.context.repository import AssetRepository, ChunkRepository
from corporate_rag_ingestion.context.settings import ContextBuilderSettings
from corporate_rag_ingestion.context.table_rendering import TableContextRenderer
from corporate_rag_ingestion.generation.ollama import OllamaClient
from corporate_rag_ingestion.generation.service import RAGAnswerService
from corporate_rag_ingestion.generation.settings import GenerationSettings
from corporate_rag_ingestion.retrieval.models import SearchResult


LEGACY_CONTEXTS = PROJECT_ROOT / "storage/reports/llm-ab-contexts.json"
PREPARED = PROJECT_ROOT / "storage/reports/table-hardening-contexts.json"
REPORT = PROJECT_ROOT / "storage/reports/table-hardening-benchmark.json"
SPACE_RE = re.compile(r"\s+")


def normalize(value: str) -> str:
    return SPACE_RE.sub(" ", value.casefold().replace("ё", "е")).strip(" .,:;*\n\t")


def answer_terms(value: str) -> list[str]:
    cleaned = re.sub(r"[•<>]", " ", value)
    emails = re.findall(r"[\w.+'-]+@[\w.-]+", cleaned)
    words = [word for word in re.findall(r"[\w.-]+", cleaned, re.UNICODE) if len(word) >= 3]
    if emails:
        return emails[:2]
    significant = [word for word in words if word.casefold() not in {"эл.почта", "электронная", "почта", "тел"}]
    return significant[:3] or words[:2] or [value]


def result_for(chunk: Any) -> SearchResult:
    return SearchResult(
        final_rank=1,
        mode="rerank",
        final_score=1.0,
        reranker_score=1.0,
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
        provenance={},
        payload={},
    )


def rerender_legacy(builder: ContextBuilder, repo: ChunkRepository) -> dict[str, Any]:
    raw = json.loads(LEGACY_CONTEXTS.read_text(encoding="utf-8"))
    output: dict[str, Any] = {}
    for label, item in raw["cases"].items():
        context = ContextPackage.model_validate(item["context"])
        for source in context.sources:
            # The old controlled fixture was deliberately created before
            # TableRepresentation existed and stores the table as positional
            # JSON in source.text. Recover that structure only for benchmark
            # rendering; no RetrievalChunk or persisted ContextPackage is changed.
            if source.table is None and source.chunk_type == "table":
                match = re.search(r"Table:\s*(\{.*\})\s*$", source.text, re.DOTALL)
                if match:
                    try:
                        positional = json.loads(match.group(1))
                    except json.JSONDecodeError:
                        positional = None
                    if isinstance(positional, dict):
                        source.table = TableRepresentation(
                            headers=[str(value) for value in positional.get("headers", [])],
                            rows=[
                                [str(value) for value in row]
                                for row in positional.get("rows", [])
                                if isinstance(row, list)
                            ],
                        )
            if source.table is None:
                continue
            candidates = repo.table_header_candidates(
                source.document_id, source.version_id, source.related_table_ids
            )
            headers, reason = builder.table_renderer.resolve_headers(
                source.table.headers, source.table.rows, source.text, candidates
            )
            source.table.render_headers = headers
            source.table.render_header_source = reason
        rendered = builder._render(context.sources)
        hardened = context.model_copy(
            update={
                "builder_version": builder.settings.builder_version,
                "settings_hash": builder.settings.settings_hash,
                "rendered_context": rendered,
                "token_count": builder.token_counter.count(rendered),
            }
        )
        output[label] = {
            "case": item["case"],
            "controlled": item.get("controlled", False),
            "context": hardened.model_dump(mode="json"),
        }
    return output


def effective(repo: ChunkRepository, renderer: TableContextRenderer, chunk: Any) -> tuple[list[str], list[list[str]]]:
    rows = [[str(value) for value in row] for row in chunk.metadata.get("table_rows") or []]
    headers, _ = renderer.resolve_headers(
        [str(value) for value in chunk.metadata.get("table_headers") or []],
        rows,
        chunk.text,
        repo.table_header_candidates(chunk.document_id, chunk.version_id, chunk.related_table_ids),
    )
    return headers, rows


def add_case(
    cases: list[dict[str, Any]],
    *,
    label: str,
    category: str,
    query: str,
    chunk: Any,
    context: ContextPackage,
    row_index: int,
    column_index: int,
    expected: str,
    headers: list[str],
    rows: list[list[str]],
) -> None:
    # The ContextPackage query is the user question consumed by the generation
    # service. The source-selection query used while constructing this controlled
    # benchmark must never leak into the generation prompt.
    context = context.model_copy(update={"query": query})
    if not expected.strip():
        expected_terms = ["не указан"]
        empty = True
    else:
        expected_terms = answer_terms(expected)
        empty = False
    other_columns = [
        value for index, value in enumerate(rows[row_index])
        if index != column_index and value.strip() and len(value.strip()) <= 100
    ]
    other_rows = [
        row[column_index] for index, row in enumerate(rows)
        if index != row_index and column_index < len(row) and row[column_index].strip()
    ]
    before_parts: list[str] = []
    for source in context.sources:
        location = source.relative_path
        if source.page_start is not None:
            location += f", pages {source.page_start}"
        header = f"{source.citation} {location}"
        if source.table is not None:
            positional = json.dumps(
                {"headers": source.table.headers, "rows": source.table.rows},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            before_parts.append(f"{header}\nTable: {positional}")
        else:
            before_parts.append(f"{header}\n{source.text}")
    before_rendered = "\n\n".join(before_parts)
    before_context = context.model_copy(
        update={
            "builder_version": "context-v1.0-positional-json",
            "rendered_context": before_rendered,
            "token_count": max(1, len(before_rendered.split())),
        }
    )
    cases.append(
        {
            "label": label,
            "category": category,
            "query": query,
            "chunk_id": chunk.chunk_id,
            "relative_path": chunk.relative_path,
            "source_type": chunk.source_type,
            "row": chunk.metadata.get("row_start"),
            "column": headers[column_index] if column_index < len(headers) else f"Колонка {column_index + 1}",
            "expected": expected,
            "expected_terms": expected_terms,
            "empty_expected": empty,
            "other_column_values": other_columns,
            "other_row_target_values": other_rows,
            "context_before": before_context.model_dump(mode="json"),
            "context": context.model_dump(mode="json"),
        }
    )


def prepare() -> dict[str, Any]:
    project = Settings()
    repo = ChunkRepository(project.chunk_storage_root)
    builder = ContextBuilder(
        repo,
        AssetRepository(project.normalized_root, project.knowledge_base_root),
        settings=ContextBuilderSettings(max_context_tokens=6000, max_chunks=10),
    )
    renderer = builder.table_renderer
    chunks = [chunk for chunk in repo.all() if chunk.chunk_type == "table" and chunk.metadata.get("table_rows")]
    table_cases: list[dict[str, Any]] = []

    # 24 unambiguous GOZ column/row cases.
    goz = [chunk for chunk in chunks if "275 " in chunk.relative_path]
    for chunk in goz:
        headers, rows = effective(repo, renderer, chunk)
        if len(headers) < 9:
            continue
        context = builder.build("table benchmark", [result_for(chunk)])
        for row_index, row in enumerate(rows):
            if len(row) < 9:
                continue
            action = row[4].strip()
            if not action or len(action) > 180 or action.casefold() == "действие":
                continue
            choices = (
                (6, "goz_responsible", f"Кто указан ответственным за действие «{action}»?"),
                (7, "goz_recipient", f"Кому направляется информация при действии «{action}»?"),
                (8, "goz_channel", f"Какой канал информации указан для действия «{action}»?"),
            )
            for column, category, query in choices:
                value = row[column].strip()
                if len(value) > 90:
                    continue
                if not value and category != "goz_recipient":
                    continue
                target = {"goz_responsible": 10, "goz_recipient": 7, "goz_channel": 7}[category]
                if sum(item["category"] == category for item in table_cases) >= target:
                    continue
                add_case(
                    table_cases,
                    label=f"{category}_{len(table_cases)+1:02d}", category=category,
                    query=query, chunk=chunk, context=context, row_index=row_index,
                    column_index=column, expected=value, headers=headers, rows=rows,
                )

    # 12 contact directory cases (person and communication channel).
    contacts = [chunk for chunk in chunks if "К кому по какому вопросу" in chunk.relative_path]
    for chunk in contacts:
        headers, rows = effective(repo, renderer, chunk)
        folded = [header.casefold() for header in headers]
        try:
            topic_col = next(index for index, value in enumerate(folded) if "тема обращения" in value)
            person_col = next(index for index, value in enumerate(folded) if "к кому обращаться" in value)
            method_col = next(index for index, value in enumerate(folded) if "способ связи" in value)
        except StopIteration:
            continue
        context = builder.build("table benchmark", [result_for(chunk)])
        for row_index, row in enumerate(rows):
            if max(topic_col, person_col, method_col) >= len(row):
                continue
            topic = row[topic_col].strip()
            if not topic or len(topic) > 130:
                continue
            person = row[person_col].strip()
            method = row[method_col].strip()
            for column, category, query in (
                (
                    person_col,
                    "contact_person",
                    f"К кому обращаться по вопросу «{topic}», если способ связи: «{method}»?",
                ),
                (
                    method_col,
                    "contact_method",
                    f"Каким способом связаться с «{person}» по вопросу «{topic}»?",
                ),
            ):
                limit = 7 if category == "contact_person" else 5
                if sum(item["category"] == category for item in table_cases) >= limit:
                    continue
                value = row[column].strip()
                if not value or len(value) > 110 or not person or not method:
                    continue
                add_case(
                    table_cases,
                    label=f"{category}_{len(table_cases)+1:02d}", category=category,
                    query=query, chunk=chunk, context=context, row_index=row_index,
                    column_index=column, expected=value, headers=headers, rows=rows,
                )

    # Four numeric XLSX cell cases outside GOZ/contact sheets.
    for chunk in chunks:
        if Path(chunk.relative_path).suffix.casefold() != ".xlsx" or chunk in goz or chunk in contacts:
            continue
        headers, rows = effective(repo, renderer, chunk)
        if len(headers) < 2 or len(headers) > 8:
            continue
        context = builder.build("table benchmark", [result_for(chunk)])
        for row_index, row in enumerate(rows):
            if len(row) < 2:
                continue
            key = next((value.strip() for value in row if value.strip() and any(char.isalpha() for char in value) and len(value) <= 70), "")
            if not key:
                continue
            key_col = row.index(key)
            for column, value in enumerate(row):
                value = value.strip()
                if column == key_col or not value or not any(char.isdigit() for char in value) or len(value) > 45:
                    continue
                query = f"Какое значение поля «{headers[column]}» указано для строки «{key}»?"
                add_case(
                    table_cases,
                    label=f"xlsx_numeric_{len(table_cases)+1:02d}", category="xlsx_numeric",
                    query=query, chunk=chunk, context=context, row_index=row_index,
                    column_index=column, expected=value, headers=headers, rows=rows,
                )
                break
            if sum(item["category"] == "xlsx_numeric" for item in table_cases) >= 4:
                break
        if sum(item["category"] == "xlsx_numeric" for item in table_cases) >= 4:
            break

    # Two manually auditable OCR table rows with clear parameter/value semantics.
    ocr_specs = {
        "d71b2dba-7c18-508d-a396-7c1fc4eeb081": (
            0, 1, "Какое отклонение указано при высоте или ширине профиля более 100 мм?"
        ),
        "7c4eb951-c0f7-55a2-9c54-145e7dd13338": (
            0, 0, "Какой диапазон радиуса указан при толщине стенки свыше 6 до 10 мм?"
        ),
    }
    for chunk in chunks:
        spec = ocr_specs.get(chunk.chunk_id)
        if spec is None:
            continue
        row_index, column_index, query = spec
        headers, rows = effective(repo, renderer, chunk)
        if row_index >= len(rows) or column_index >= len(rows[row_index]):
            continue
        context = builder.build("table benchmark", [result_for(chunk)])
        add_case(
            table_cases,
            label=f"ocr_table_{len(table_cases)+1:02d}", category="ocr_table",
            query=query, chunk=chunk, context=context, row_index=row_index,
            column_index=column_index, expected=rows[row_index][column_index],
            headers=headers, rows=rows,
        )

    # Remove exact duplicate question/answer pairs while preserving stable order.
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for case in table_cases:
        key = (normalize(case["query"]), normalize(case["expected"]))
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(case)
    table_cases = deduplicated

    payload = {
        "schema_version": "1.0",
        "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "builder_version": builder.settings.builder_version,
        "legacy_cases": rerender_legacy(builder, repo),
        "table_cases": table_cases,
    }
    PREPARED.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def evaluate(case: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    text = normalize(answer["answer"])
    expected_terms = [normalize(value) for value in case["expected_terms"]]
    correct = all(term in text for term in expected_terms)
    if case["empty_expected"]:
        correct = any(marker in text for marker in ("не указан", "нет информац", "не содерж"))
    column_hits = [value for value in case["other_column_values"] if len(normalize(value)) >= 4 and normalize(value) in text]
    row_hits = [value for value in case["other_row_target_values"] if len(normalize(value)) >= 4 and normalize(value) in text]
    return {
        "correct": correct,
        "column_error": bool(not correct and column_hits),
        "row_error": bool(not correct and row_hits),
        "empty_cell_hallucination": bool(case["empty_expected"] and not correct),
        "citation_error": answer["citation_validation"] != "valid",
        "column_distractors_found": column_hits,
        "row_distractors_found": row_hits,
    }


def run(models: list[str]) -> dict[str, Any]:
    prepared = json.loads(PREPARED.read_text(encoding="utf-8"))
    report: dict[str, Any] = {"prepared_at": prepared["prepared_at"], "models": {}}
    checkpoint = REPORT.with_name("table-hardening-benchmark.checkpoint.json")
    if checkpoint.exists():
        loaded = json.loads(checkpoint.read_text(encoding="utf-8"))
        if loaded.get("prepared_at") == prepared["prepared_at"]:
            report = loaded
    for model in models:
        settings = GenerationSettings(model=model, num_ctx=12288, think=False, stream=False, temperature=0.1, keep_alive="60m", timeout_seconds=120)
        existing = report["models"].get(model, {})
        legacy_results: dict[str, Any] = existing.get("legacy_cases", {})
        table_results: dict[str, Any] = existing.get("table_cases", {})
        latencies: list[float] = existing.get("successful_latencies", [])
        with OllamaClient(settings) as client:
            service = RAGAnswerService(client, settings)
            for label, item in prepared["legacy_cases"].items():
                if label in legacy_results:
                    continue
                try:
                    answer = service.generate(ContextPackage.model_validate(item["context"]))
                    legacy_results[label] = answer.model_dump(mode="json")
                    latencies.append(answer.latency.llm_seconds)
                    print(json.dumps({"model": model, "set": "legacy", "case": label, "seconds": answer.latency.llm_seconds}, ensure_ascii=False), flush=True)
                except Exception as exc:  # live benchmark must retain completed work
                    legacy_results[label] = {"benchmark_error": f"{type(exc).__name__}: {exc}"}
                    print(json.dumps({"model": model, "set": "legacy", "case": label, "error": str(exc)}, ensure_ascii=False), flush=True)
                report["models"][model] = {"legacy_cases": legacy_results, "table_cases": table_results, "successful_latencies": latencies}
                checkpoint.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            for case in prepared["table_cases"]:
                stored = table_results.get(case["label"], {})
                case_results: dict[str, Any] = {
                    key: value for key, value in stored.items() if key in {"before", "after"}
                }
                for representation, context_key in (("before", "context_before"), ("after", "context")):
                    if representation in case_results:
                        continue
                    try:
                        answer = service.generate(ContextPackage.model_validate(case[context_key]))
                        dumped = answer.model_dump(mode="json")
                        dumped["evaluation"] = evaluate(case, dumped)
                        case_results[representation] = dumped
                        latencies.append(answer.latency.llm_seconds)
                        print(json.dumps({"model": model, "set": "table", "representation": representation, "case": case["label"], "correct": dumped["evaluation"]["correct"], "seconds": answer.latency.llm_seconds}, ensure_ascii=False), flush=True)
                    except Exception as exc:  # live benchmark must retain completed work
                        case_results[representation] = {"benchmark_error": f"{type(exc).__name__}: {exc}"}
                        print(json.dumps({"model": model, "set": "table", "representation": representation, "case": case["label"], "error": str(exc)}, ensure_ascii=False), flush=True)
                    table_results[case["label"]] = {
                        "case": {key: value for key, value in case.items() if key not in {"context", "context_before"}},
                        **case_results,
                    }
                    report["models"][model] = {"legacy_cases": legacy_results, "table_cases": table_results, "successful_latencies": latencies}
                    checkpoint.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                table_results[case["label"]] = {
                    "case": {key: value for key, value in case.items() if key not in {"context", "context_before"}},
                    **case_results,
                }
            runtime_models: list[dict[str, Any]] = []
            try:
                runtime = client._client.get("/api/ps")  # benchmark diagnostics only
                runtime.raise_for_status()
                runtime_models = runtime.json().get("models", [])
            except Exception as exc:  # pragma: no cover - live diagnostic
                runtime_models = [{"diagnostic_error": str(exc)}]
        evaluations_by_representation = {
            representation: [
                item[representation]["evaluation"] for item in table_results.values()
                if "evaluation" in item.get(representation, {})
            ]
            for representation in ("before", "after")
        }
        metrics: dict[str, Any] = {}
        for representation, evaluations in evaluations_by_representation.items():
            metrics[representation] = {
                "accuracy": sum(item["correct"] for item in evaluations) / len(evaluations) if evaluations else None,
                "evaluated": len(evaluations),
                "correct": sum(item["correct"] for item in evaluations),
                "column_errors": sum(item["column_error"] for item in evaluations),
                "row_errors": sum(item["row_error"] for item in evaluations),
                "empty_cell_hallucinations": sum(item["empty_cell_hallucination"] for item in evaluations),
                "citation_errors": sum(item["citation_error"] for item in evaluations),
            }
        report["models"][model] = {
            "settings_hash": settings.settings_hash,
            "legacy_cases": legacy_results,
            "table_cases": table_results,
            "table_case_count": len(table_results),
            "metrics": metrics,
            "average_seconds": statistics.fmean(latencies),
            "median_seconds": statistics.median(latencies),
            "ollama_runtime_models": runtime_models,
        }
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        checkpoint.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "run"])
    parser.add_argument("--models", nargs="+", default=["qwen3:14b", "qwen3:8b"])
    args = parser.parse_args()
    if args.command == "prepare":
        payload = prepare()
        print(json.dumps({"legacy": len(payload["legacy_cases"]), "tables": len(payload["table_cases"]), "output": str(PREPARED)}, ensure_ascii=False))
    else:
        report = run(args.models)
        print(json.dumps({model: {key: value for key, value in data.items() if key not in {"legacy_cases", "table_cases"}} for model, data in report["models"].items()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
