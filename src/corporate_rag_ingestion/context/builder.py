from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Sequence

from ..chunking.models import RetrievalChunk
from ..retrieval.models import SearchResult
from .models import (
    ContextBuildStats,
    ContextPackage,
    ContextSource,
    RelatedImage,
    RetrievalSignals,
    TableRepresentation,
)
from .repository import AssetRepository, ChunkRepository
from .settings import ContextBuilderSettings
from .tokens import ContextTokenCounter, StableTokenCounter
from .table_rendering import TableContextRenderer


WORD_RE = re.compile(r"\w+", re.UNICODE)
SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class _Selected:
    chunk: RetrievalChunk
    result: SearchResult
    added_as_neighbor: bool = False
    parent_chunk_id: str | None = None


class ContextBuilder:
    def __init__(
        self,
        chunks: ChunkRepository,
        assets: AssetRepository,
        *,
        settings: ContextBuilderSettings | None = None,
        token_counter: ContextTokenCounter | None = None,
    ) -> None:
        self.chunks = chunks
        self.assets = assets
        self.settings = settings or ContextBuilderSettings()
        self.token_counter = token_counter or StableTokenCounter()
        self.table_renderer = TableContextRenderer()

    def build(self, query: str, results: Sequence[SearchResult]) -> ContextPackage:
        stats = ContextBuildStats(input_results=len(results))
        primaries: list[_Selected] = []
        document_counts: Counter[str] = Counter()
        for result in sorted(results, key=lambda item: (item.final_rank, item.chunk_id)):
            chunk = self.chunks.get(result.chunk_id) or self._chunk_from_result(result)
            duplicate_kind = self._duplicate_kind(chunk.text, [item.chunk.text for item in primaries])
            if duplicate_kind == "exact":
                stats.exact_duplicates_removed += 1
                continue
            if duplicate_kind == "near":
                stats.near_duplicates_removed += 1
                continue
            if document_counts[chunk.document_id] >= self.settings.max_chunks_per_document:
                stats.document_limit_removed += 1
                continue
            candidate = _Selected(chunk=chunk, result=result)
            if not self._fits([*primaries, candidate]):
                stats.budget_removed += 1
                continue
            primaries.append(candidate)
            document_counts[chunk.document_id] += 1
            if len(primaries) >= self.settings.max_chunks:
                break
        selected = self._expand_neighbors(primaries, document_counts, stats)
        stats.primary_sources = sum(not item.added_as_neighbor for item in selected)
        sources = [self._source(item, number) for number, item in enumerate(selected, start=1)]
        rendered = self._render(sources)
        related_images, missing_count = self._related_images(sources)
        stats.missing_assets_removed = missing_count
        context_id = self._context_id(query, selected)
        return ContextPackage(
            query=query,
            context_id=context_id,
            builder_version=self.settings.builder_version,
            settings_hash=self.settings.settings_hash,
            token_counter=self.token_counter.method,
            token_count=self.token_counter.count(rendered),
            rendered_context=rendered,
            sources=sources,
            related_images=related_images,
            stats=stats,
        )

    def _expand_neighbors(
        self,
        primaries: list[_Selected],
        document_counts: Counter[str],
        stats: ContextBuildStats,
    ) -> list[_Selected]:
        if self.settings.neighbor_expansion_limit == 0:
            return list(primaries)
        base = list(primaries)
        additions: dict[str, _Selected] = {}
        added = 0
        all_texts = [item.chunk.text for item in primaries]
        primary_ids = {item.chunk.chunk_id for item in primaries}
        for primary in primaries:
            if primary not in base:
                continue
            if added >= self.settings.neighbor_expansion_limit or not self._needs_neighbor(primary.chunk):
                continue
            for neighbor_id in (primary.chunk.previous_chunk_id, primary.chunk.next_chunk_id):
                if not neighbor_id or neighbor_id in primary_ids:
                    continue
                neighbor = self.chunks.get(neighbor_id)
                if neighbor is None or not self._valid_neighbor(primary.chunk, neighbor):
                    continue
                if self._duplicate_kind(neighbor.text, all_texts) is not None:
                    continue
                selected = _Selected(
                    chunk=neighbor,
                    result=primary.result,
                    added_as_neighbor=True,
                    parent_chunk_id=primary.chunk.chunk_id,
                )
                tentative_base = list(base)
                needs_document_slot = (
                    document_counts[neighbor.document_id]
                    >= self.settings.max_chunks_per_document
                )
                needs_global_slot = len(base) + len(additions) >= self.settings.max_chunks
                removed: _Selected | None = None
                if needs_document_slot or needs_global_slot:
                    removable = [
                        item
                        for item in reversed(tentative_base)
                        if item.chunk.chunk_id != primary.chunk.chunk_id
                        and item.result.final_rank > primary.result.final_rank
                        and (
                            not needs_document_slot
                            or item.chunk.document_id == neighbor.document_id
                        )
                    ]
                    if not removable:
                        continue
                    removed = removable[0]
                    tentative_base.remove(removed)
                tentative_additions = {**additions, primary.chunk.chunk_id: selected}
                provisional = self._assemble(tentative_base, tentative_additions)
                if not self._fits(provisional):
                    continue
                base = tentative_base
                additions = tentative_additions
                if removed is not None:
                    document_counts[removed.chunk.document_id] -= 1
                    stats.lower_rank_results_replaced_by_neighbors += 1
                all_texts.append(neighbor.text)
                document_counts[neighbor.document_id] += 1
                added += 1
                break
        stats.neighbors_added = added
        return self._assemble(base, additions)

    @staticmethod
    def _assemble(
        primaries: list[_Selected], additions: dict[str, _Selected]
    ) -> list[_Selected]:
        output: list[_Selected] = []
        for primary in primaries:
            output.append(primary)
            neighbor = additions.get(primary.chunk.chunk_id)
            if neighbor is not None:
                output.append(neighbor)
        return output

    def _needs_neighbor(self, chunk: RetrievalChunk) -> bool:
        if chunk.chunk_type != "text" or chunk.token_count > self.settings.neighbor_trigger_max_tokens:
            return False
        if chunk.metadata.get("split_source_parts"):
            return True
        text = chunk.text.rstrip()
        if text.endswith((",", ";", ":", "-", "—", "(", "[")):
            return True
        return chunk.token_count < 80 and not text.endswith((".", "!", "?", "]", ")", "»"))

    def _valid_neighbor(self, parent: RetrievalChunk, neighbor: RetrievalChunk) -> bool:
        if neighbor.document_id != parent.document_id or neighbor.version_id != parent.version_id:
            return False
        if neighbor.metadata.get("retrieval_candidate") is False:
            return False
        if neighbor.token_count > self.settings.neighbor_max_tokens:
            return False
        if parent.parent_section_id and neighbor.parent_section_id:
            return parent.parent_section_id == neighbor.parent_section_id
        if set(parent.heading_path) & set(neighbor.heading_path):
            return True
        # A direct text -> table sequence introduced by a colon is a strong
        # structural relation even in spreadsheets that have no page/section IDs.
        return neighbor.chunk_type == "table" and parent.text.rstrip().endswith(":")

    def _fits(self, selected: list[_Selected]) -> bool:
        if len(selected) > self.settings.max_chunks:
            return False
        provisional = [self._source(item, number) for number, item in enumerate(selected, start=1)]
        return self.token_counter.count(self._render(provisional)) <= self.settings.max_context_tokens

    def _source(self, selected: _Selected, number: int) -> ContextSource:
        chunk = selected.chunk
        result = selected.result
        table = None
        if chunk.chunk_type == "table":
            raw_headers = list(chunk.metadata.get("table_headers") or [])
            raw_rows = [list(row) for row in (chunk.metadata.get("table_rows") or [])]
            candidate_rows: list[list[str]] = []
            candidate_loader = getattr(self.chunks, "table_header_candidates", None)
            if callable(candidate_loader):
                candidate_rows = candidate_loader(
                    chunk.document_id,
                    chunk.version_id,
                    list(chunk.related_table_ids),
                )
            render_headers, render_header_source = self.table_renderer.resolve_headers(
                raw_headers,
                raw_rows,
                chunk.text,
                candidate_rows,
            )
            table = TableRepresentation(
                headers=raw_headers,
                rows=raw_rows,
                html=chunk.metadata.get("table_html"),
                original_html=chunk.metadata.get("original_table_html"),
                row_start=chunk.metadata.get("row_start"),
                row_end=chunk.metadata.get("row_end"),
                render_headers=render_headers,
                render_header_source=render_header_source,
            )
        provenance = dict(result.provenance)
        for key, value in chunk.metadata.items():
            if (
                key.startswith("ocr_")
                or key.startswith("source_fallback")
                or key == "sheet_name"
            ):
                provenance.setdefault(key, value)
        return ContextSource(
            source_number=number,
            citation=f"[{number}]",
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            version_id=chunk.version_id,
            relative_path=chunk.relative_path,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            heading_path=list(chunk.heading_path),
            section_title=chunk.section_title,
            parent_section_id=chunk.parent_section_id,
            text=chunk.text,
            chunk_type=chunk.chunk_type,
            table=table,
            retrieval=RetrievalSignals(
                reranker_rank=result.final_rank,
                reranker_score=result.reranker_score,
                rrf_rank=result.rrf_rank,
                rrf_score=result.rrf_score,
                dense_rank=result.dense_rank,
                dense_score=result.dense_score,
                sparse_rank=result.sparse_rank,
                sparse_score=result.sparse_score,
            ),
            source_type=chunk.source_type,
            provenance=provenance,
            source_element_ids=list(chunk.source_element_ids),
            related_image_ids=list(chunk.related_image_ids),
            related_table_ids=list(chunk.related_table_ids),
            added_as_neighbor=selected.added_as_neighbor,
            parent_chunk_id=selected.parent_chunk_id,
        )

    def _related_images(self, sources: list[ContextSource]) -> tuple[list[RelatedImage], int]:
        found: list[RelatedImage] = []
        seen: set[str] = set()
        missing = 0
        for source in sources:
            images, absent = self.assets.resolve(
                source.document_id, source.version_id, source.related_image_ids
            )
            missing += len(absent)
            valid_ids: list[str] = []
            for image in images:
                valid_ids.append(image.asset_id)
                if image.asset_id not in seen:
                    found.append(image)
                    seen.add(image.asset_id)
            source.related_image_ids = valid_ids
        return found, missing

    def _render(self, sources: list[ContextSource]) -> str:
        parts: list[str] = []
        for source in sources:
            location = source.relative_path
            if source.page_start is not None:
                pages = str(source.page_start)
                if source.page_end and source.page_end != source.page_start:
                    pages += f"-{source.page_end}"
                location += f", pages {pages}"
            heading = " > ".join(source.heading_path)
            header = f"{source.citation} {location}"
            if heading:
                header += f"\nSection: {heading}"
            if source.added_as_neighbor:
                header += f"\nNeighbor of chunk: {source.parent_chunk_id}"
            if source.provenance.get("source_fallback") == "standalone_visual_filename":
                header += (
                    "\nSource kind: standalone visual; only its filename is searchable; "
                    "the original image is attached"
                )
            if source.chunk_type == "table" and source.table is not None:
                structured = self.table_renderer.render(
                    source.table,
                    source.text,
                    citation=source.citation,
                )
                parts.append(f"{header}\n{structured}")
            else:
                parts.append(f"{header}\n{source.text}")
        return "\n\n".join(parts)

    def _context_id(self, query: str, selected: list[_Selected]) -> str:
        payload = {
            "query": query,
            "builder_version": self.settings.builder_version,
            "settings_hash": self.settings.settings_hash,
            "sources": [
                {
                    "chunk_id": item.chunk.chunk_id,
                    "neighbor": item.added_as_neighbor,
                    "parent": item.parent_chunk_id,
                }
                for item in selected
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    def _duplicate_kind(self, text: str, existing: list[str]) -> str | None:
        normalized = self._normalize(text)
        for other in existing:
            other_normalized = self._normalize(other)
            if normalized == other_normalized:
                return "exact"
            if self._near_duplicate(normalized, other_normalized):
                return "near"
        return None

    @staticmethod
    def _normalize(text: str) -> str:
        return SPACE_RE.sub(" ", text.casefold()).strip()

    def _near_duplicate(self, left: str, right: str) -> bool:
        left_words = WORD_RE.findall(left)
        right_words = WORD_RE.findall(right)
        if min(len(left_words), len(right_words)) < self.settings.near_duplicate_min_words:
            return False
        left_shingles = set(zip(left_words, left_words[1:], left_words[2:]))
        right_shingles = set(zip(right_words, right_words[1:], right_words[2:]))
        if not left_shingles or not right_shingles:
            return False
        return len(left_shingles & right_shingles) / len(left_shingles | right_shingles) >= self.settings.near_duplicate_threshold

    @staticmethod
    def _chunk_from_result(result: SearchResult) -> RetrievalChunk:
        payload: dict[str, Any] = result.payload
        source_ids = payload.get("source_element_ids") or [f"qdrant:{result.chunk_id}"]
        return RetrievalChunk(
            chunk_id=result.chunk_id,
            document_id=result.document_id,
            version_id=result.version_id,
            relative_path=result.relative_path,
            chunk_type=result.chunk_type,
            text=result.text,
            token_count=max(1, int(payload.get("structural_token_count") or 1)),
            page_start=result.page_start,
            page_end=result.page_end,
            heading_path=result.heading_path,
            section_title=result.section_title,
            parent_section_id=payload.get("parent_section_id"),
            source_element_ids=source_ids,
            previous_chunk_id=payload.get("previous_chunk_id"),
            next_chunk_id=payload.get("next_chunk_id"),
            related_image_ids=result.related_image_ids,
            related_table_ids=payload.get("related_table_ids") or [],
            source_type=result.source_type,
            metadata=payload.get("chunk_metadata") or {},
        )
