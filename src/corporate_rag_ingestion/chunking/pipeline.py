from __future__ import annotations

import statistics
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schemas import NormalizedDocument
from .chunker import StructuralChunker
from .overrides import ChunkingOverrides
from .settings import ChunkingSettings
from .storage import ChunkStorage
from .validation import validate_chunked_document


@dataclass(slots=True)
class ChunkingSummary:
    normalized_documents: int = 0
    processed_documents: int = 0
    zero_chunk_documents: int = 0
    written_documents: int = 0
    unchanged_documents: int = 0
    chunks: int = 0
    text_chunks: int = 0
    table_chunks: int = 0
    chunks_with_images: int = 0
    ocr_chunks: int = 0
    mixed_source_chunks: int = 0
    native_fallback_documents: int = 0
    native_fallback_pages: int = 0
    eligible_source_elements: int = 0
    covered_source_elements: int = 0
    missing_source_elements: int = 0
    above_hard_max: int = 0
    hard_max_reasons: dict[str, int] = field(default_factory=dict)
    source_types: dict[str, int] = field(default_factory=dict)
    token_stats: dict[str, float | int] = field(default_factory=dict)
    errors: int = 0
    error_details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "normalized_documents": self.normalized_documents,
            "processed_documents": self.processed_documents,
            "zero_chunk_documents": self.zero_chunk_documents,
            "written_documents": self.written_documents,
            "unchanged_documents": self.unchanged_documents,
            "chunks": self.chunks,
            "text_chunks": self.text_chunks,
            "table_chunks": self.table_chunks,
            "chunks_with_images": self.chunks_with_images,
            "ocr_chunks": self.ocr_chunks,
            "mixed_source_chunks": self.mixed_source_chunks,
            "native_fallback_documents": self.native_fallback_documents,
            "native_fallback_pages": self.native_fallback_pages,
            "eligible_source_elements": self.eligible_source_elements,
            "covered_source_elements": self.covered_source_elements,
            "missing_source_elements": self.missing_source_elements,
            "above_hard_max": self.above_hard_max,
            "hard_max_reasons": self.hard_max_reasons,
            "source_types": self.source_types,
            "token_stats": self.token_stats,
            "errors": self.errors,
            "error_details": self.error_details,
        }


class ChunkingPipeline:
    def __init__(
        self,
        *,
        normalized_root: Path,
        chunk_storage: ChunkStorage,
        settings: ChunkingSettings | None = None,
        overrides: ChunkingOverrides | None = None,
    ) -> None:
        self.normalized_root = normalized_root.resolve()
        self.storage = chunk_storage
        self.settings = settings or ChunkingSettings()
        self.overrides = overrides or ChunkingOverrides()
        self.chunker = StructuralChunker(self.settings, self.overrides)

    def run(self) -> ChunkingSummary:
        summary = ChunkingSummary()
        tokens: list[int] = []
        source_types: Counter[str] = Counter()
        hard_reasons: Counter[str] = Counter()
        paths = sorted(self.normalized_root.rglob("*.json"))
        summary.normalized_documents = len(paths)
        for path in paths:
            try:
                document = NormalizedDocument.model_validate_json(path.read_text(encoding="utf-8"))
                chunked = self.chunker.chunk(document)
                validate_chunked_document(document, chunked, self.settings)
                _, changed = self.storage.save(chunked)
                summary.processed_documents += 1
                summary.written_documents += int(changed)
                summary.unchanged_documents += int(not changed)
                summary.zero_chunk_documents += int(not chunked.chunks)
                summary.chunks += len(chunked.chunks)
                summary.text_chunks += sum(chunk.chunk_type == "text" for chunk in chunked.chunks)
                summary.table_chunks += sum(chunk.chunk_type == "table" for chunk in chunked.chunks)
                summary.chunks_with_images += sum(bool(chunk.related_image_ids) for chunk in chunked.chunks)
                summary.ocr_chunks += sum(chunk.source_type in {"ocr", "mixed"} for chunk in chunked.chunks)
                summary.mixed_source_chunks += sum(chunk.source_type == "mixed" for chunk in chunked.chunks)
                derived_pages = chunked.metadata.get("derived_native_page_sources", [])
                summary.native_fallback_documents += int(bool(derived_pages))
                summary.native_fallback_pages += len(derived_pages)
                summary.eligible_source_elements += chunked.coverage.eligible_source_elements
                summary.covered_source_elements += chunked.coverage.covered_source_elements
                summary.missing_source_elements += len(chunked.coverage.missing_source_element_ids)
                for chunk in chunked.chunks:
                    tokens.append(chunk.token_count)
                    source_types[chunk.source_type] += 1
                    if chunk.token_count > self.settings.hard_max_tokens:
                        summary.above_hard_max += 1
                        hard_reasons[str(chunk.metadata.get("hard_max_exception") or "unknown")] += 1
            except Exception as exc:
                summary.errors += 1
                summary.error_details.append(f"{path}: {type(exc).__name__}: {exc}")
        summary.source_types = dict(sorted(source_types.items()))
        summary.hard_max_reasons = dict(sorted(hard_reasons.items()))
        summary.token_stats = self._token_stats(tokens)
        return summary

    @staticmethod
    def _token_stats(tokens: list[int]) -> dict[str, float | int]:
        if not tokens:
            return {"min": 0, "avg": 0.0, "median": 0.0, "p95": 0.0, "max": 0}
        ordered = sorted(tokens)
        p95_index = max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))
        return {
            "min": ordered[0],
            "avg": round(statistics.fmean(ordered), 2),
            "median": round(statistics.median(ordered), 2),
            "p95": ordered[p95_index],
            "max": ordered[-1],
        }
