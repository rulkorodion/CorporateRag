from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from corporate_rag_ingestion.config import Settings
from corporate_rag_ingestion.db import make_engine
from corporate_rag_ingestion.pipeline import NORMALIZER_VERSION
from corporate_rag_ingestion.registry_models import Document, DocumentVersion
from corporate_rag_ingestion.scanner import FileScanner
from corporate_rag_ingestion.schemas import NormalizedDocument


def page_ranges(numbers: Iterable[int]) -> str:
    ordered = sorted(set(numbers))
    ranges: list[str] = []
    start = end = ordered[0]
    for number in ordered[1:]:
        if number == end + 1:
            end = number
            continue
        ranges.append(str(start) if start == end else f"{start}-{end}")
        start = end = number
    ranges.append(str(start) if start == end else f"{start}-{end}")
    return ", ".join(ranges)


def main() -> None:
    settings = Settings()
    normalized_paths = sorted(settings.normalized_root.rglob("*.json"))
    normalized: list[NormalizedDocument] = []
    invalid: list[dict[str, str]] = []
    for path in normalized_paths:
        try:
            normalized.append(
                NormalizedDocument.model_validate_json(path.read_text(encoding="utf-8"))
            )
        except Exception as exc:
            invalid.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})

    engine = make_engine(settings.database_url)
    with Session(engine) as session:
        documents = session.execute(select(Document)).scalars().all()
        versions = session.execute(select(DocumentVersion)).scalars().all()

    registered_hashes = {document.relative_path: document.current_sha256 for document in documents}
    source_hash_mismatches = [
        item.relative_path
        for item in FileScanner(settings.knowledge_base_root).scan()
        if registered_hashes.get(item.relative_path) != item.sha256
    ]

    pdf_documents = [doc for doc in normalized if doc.metadata.get("extension") == ".pdf"]
    pdf_pages = [page for doc in pdf_documents for page in doc.pages]
    page_types = Counter(page.page_visual_type for page in pdf_pages)
    scan_candidates: dict[str, list[int]] = defaultdict(list)
    ambiguous: dict[str, list[int]] = defaultdict(list)
    for doc in pdf_documents:
        relative_path = str(doc.metadata.get("relative_path", doc.document_id))
        for page in doc.pages:
            if page.page_visual_type == "scan_candidate":
                scan_candidates[relative_path].append(page.page_number)
            elif page.page_visual_type == "ambiguous":
                ambiguous[relative_path].append(page.page_number)

    report = {
        "normalizer_version": NORMALIZER_VERSION,
        "documents": len(documents),
        "versions": len(versions),
        "normalized_json_valid": len(normalized),
        "normalized_json_invalid": invalid,
        "normalizer_marked_json": sum(
            doc.metadata.get("normalizer_version") == NORMALIZER_VERSION for doc in normalized
        ),
        "document_statuses": dict(Counter(document.processing_status for document in documents)),
        "version_statuses": dict(Counter(version.processing_status for version in versions)),
        "source_hash_mismatches": source_hash_mismatches,
        "pdf_page_types": {
            page_type: page_types.get(page_type, 0)
            for page_type in (
                "text",
                "mixed",
                "scan_candidate",
                "visual_or_slide",
                "ambiguous",
            )
        },
        "needs_ocr": sum(page.needs_ocr for page in pdf_pages),
        "extracted_images": sum(
            asset.type == "image" and asset.visual_role == "embedded_image"
            for doc in normalized
            for asset in doc.assets
        ),
        "related_to_relations": sum(
            relation.type == "related_to" for doc in normalized for relation in doc.relations
        ),
        "assets_with_nearby_text": sum(
            bool(asset.nearby_text_element_ids) for doc in normalized for asset in doc.assets
        ),
        "legacy": dict(
            Counter(
                str(doc.metadata.get("extension"))
                for doc in normalized
                if doc.metadata.get("extension") in {".doc", ".rtf"}
            )
        ),
        "mp4_transcript_statuses": dict(
            Counter(
                str(doc.metadata.get("transcript_status"))
                for doc in normalized
                if doc.metadata.get("extension") == ".mp4"
            )
        ),
        "scan_candidate_pages": dict(sorted(scan_candidates.items())),
        "ambiguous_pages": dict(sorted(ambiguous.items())),
    }

    report_directory = settings.storage_root / "reports"
    report_directory.mkdir(parents=True, exist_ok=True)
    (report_directory / "ingestion_validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines = ["# Ingestion validation", "", "## Scan candidate pages", ""]
    lines.extend(
        f"- `{path}`: {page_ranges(pages)}" for path, pages in sorted(scan_candidates.items())
    )
    lines.extend(["", "## Ambiguous pages", ""])
    lines.extend(f"- `{path}`: {page_ranges(pages)}" for path, pages in sorted(ambiguous.items()))
    (report_directory / "page_classification.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "normalized_json_valid": report["normalized_json_valid"],
                "normalized_json_invalid": len(invalid),
                "version_statuses": report["version_statuses"],
                "source_hash_mismatches": len(source_hash_mismatches),
                "legacy": report["legacy"],
                "reports_directory": str(report_directory),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
