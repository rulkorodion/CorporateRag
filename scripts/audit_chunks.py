from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


SEARCHABLE_TYPES = {"heading", "paragraph", "list", "table", "formula", "scanned_region"}


def _classification(
    chunk: dict[str, Any],
    source_types: list[str],
    *,
    document_chunk_count: int,
    fallback_ids: set[str],
) -> tuple[str, str]:
    text = chunk["text"].strip()
    semantic_characters = sum(character.isalnum() for character in text)
    semantic_ratio = semantic_characters / max(1, len(text))
    metadata = chunk.get("metadata") or {}
    if chunk["chunk_type"] == "table":
        if metadata.get("table_source_format") == "table_title_only":
            return (
                "table_title_only",
                "structural_noise: workbook sheet/table title has no recoverable cells",
            )
        return "small_complete_table", "useful: compact structured table is kept intact"
    if any(source_id in fallback_ids for source_id in chunk["source_element_ids"]):
        return (
            "short_native_page_fallback",
            "useful_or_review: short native page text recovered where structural elements were absent",
        )
    if semantic_characters == 0 or semantic_ratio < 0.25:
        return "low_semantic_signal", "structural_noise: mostly punctuation or extraction artifacts"
    if source_types and set(source_types) == {"heading"}:
        return (
            "heading_without_body",
            "weak_standalone: heading has no searchable body before the next structural boundary",
        )
    if document_chunk_count == 1:
        return "short_document", "useful: the complete source document is intentionally short"
    if "heading" in source_types:
        return "short_section", "useful: complete short section with its heading"
    if chunk.get("heading_path"):
        return "short_section_tail", "merge_candidate: short tail belongs to the same section"
    return "isolated_short_content", "merge_candidate: short content separated by a document boundary"


def audit(normalized_root: Path, chunks_root: Path) -> dict[str, Any]:
    normalized: dict[str, dict[str, Any]] = {}
    for path in normalized_root.rglob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        for element in document.get("elements", []):
            normalized[element["element_id"]] = element

    documents = [
        json.loads(path.read_text(encoding="utf-8")) for path in chunks_root.rglob("*.json")
    ]
    fallback_ids = {
        item["element_id"]
        for document in documents
        for item in document.get("metadata", {}).get("derived_native_page_sources", [])
    }
    chunks = [(document, chunk) for document in documents for chunk in document["chunks"]]
    thresholds = {
        f"lt_{limit}": sum(chunk["token_count"] < limit for _, chunk in chunks)
        for limit in (10, 20, 30, 50)
    }
    rows: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    assessments: Counter[str] = Counter()
    for document, chunk in chunks:
        if chunk["token_count"] >= 30:
            continue
        source_types = [
            normalized[source_id]["type"]
            for source_id in chunk["source_element_ids"]
            if source_id in normalized and normalized[source_id]["type"] in SEARCHABLE_TYPES
        ]
        reason, assessment = _classification(
            chunk,
            source_types,
            document_chunk_count=len(document["chunks"]),
            fallback_ids=fallback_ids,
        )
        reasons[reason] += 1
        assessments[assessment.split(":", 1)[0]] += 1
        rows.append(
            {
                "document": document["relative_path"],
                "page": (
                    str(chunk["page_start"])
                    if chunk["page_start"] == chunk["page_end"]
                    else f"{chunk['page_start'] or '-'}–{chunk['page_end'] or '-'}"
                ),
                "chunk_type": chunk["chunk_type"],
                "tokens": chunk["token_count"],
                "heading_or_section": " > ".join(chunk.get("heading_path") or [])
                or chunk.get("section_title")
                or "—",
                "text": chunk["text"],
                "reason": reason,
                "assessment": assessment,
                "chunk_id": chunk["chunk_id"],
            }
        )
    return {
        "total_chunks": len(chunks),
        "thresholds": thresholds,
        "reason_counts": dict(sorted(reasons.items())),
        "assessment_counts": dict(sorted(assessments.items())),
        "chunks_under_30": rows,
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# RetrievalChunk audit: chunks under 30 tokens",
        "",
        f"Total chunks: {report['total_chunks']}",
        "",
        "Thresholds: "
        + ", ".join(f"{key.replace('_', ' ')} = {value}" for key, value in report["thresholds"].items()),
        "",
        "Reason counts: "
        + ", ".join(f"{key} = {value}" for key, value in report["reason_counts"].items()),
        "",
    ]
    for index, row in enumerate(report["chunks_under_30"], start=1):
        lines.extend(
            [
                f"## {index}. {row['document']}",
                "",
                f"- Page: {row['page']}",
                f"- Type: {row['chunk_type']}",
                f"- Tokens: {row['tokens']}",
                f"- Heading/section: {row['heading_or_section']}",
                f"- Reason: `{row['reason']}`",
                f"- Assessment: {row['assessment']}",
                f"- Chunk ID: `{row['chunk_id']}`",
                "",
                "```text",
                re.sub(r"```", "` ` `", row["text"]),
                "```",
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--normalized-root", type=Path, default=Path("storage/normalized"))
    parser.add_argument("--chunks-root", type=Path, default=Path("storage/chunks"))
    parser.add_argument("--output", type=Path, default=Path("storage/reports/chunks-under-30.md"))
    args = parser.parse_args()
    report = audit(args.normalized_root, args.chunks_root)
    write_markdown(report, args.output)
    print(json.dumps({key: value for key, value in report.items() if key != "chunks_under_30"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
