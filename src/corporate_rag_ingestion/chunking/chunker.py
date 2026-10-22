from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..schemas import NormalizedDocument, NormalizedElement
from .models import ChunkCoverage, ChunkedDocument, RetrievalChunk
from .overrides import ChunkingOverrides
from .settings import ChunkingSettings
from .tables import TableChunker, TablePart
from .tokens import TokenEstimator


SEARCHABLE_TYPES = {"heading", "paragraph", "list", "table", "formula", "scanned_region"}
TEXT_TYPES = {"heading", "paragraph", "list", "formula", "scanned_region"}
NUMBERED_HEADING_RE = re.compile(r"^\s*(?:раздел\s+)?(\d+(?:\.\d+){0,5})[.)\s]", re.I)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(slots=True)
class SourceUnit:
    element: NormalizedElement
    text: str
    kind: str
    heading_path: list[str]
    heading_ids: list[str]
    parent_section_id: str | None
    source_type: str
    split_part: int | None = None


@dataclass(slots=True)
class HeadingState:
    level: int
    text: str
    element: NormalizedElement
    source_type: str


@dataclass(slots=True)
class ChunkCandidate:
    chunk_type: str
    text: str
    token_count: int
    page_start: int | None
    page_end: int | None
    heading_path: list[str]
    section_title: str | None
    parent_section_id: str | None
    source_element_ids: list[str]
    related_image_ids: list[str]
    related_table_ids: list[str]
    source_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


class StructuralChunker:
    def __init__(
        self,
        settings: ChunkingSettings | None = None,
        overrides: ChunkingOverrides | None = None,
    ) -> None:
        self.settings = settings or ChunkingSettings()
        self.overrides = overrides or ChunkingOverrides()
        self.effective_settings_hash = self.overrides.settings_hash(
            self.settings.effective_settings()
        )
        self.estimator = TokenEstimator()
        self.table_chunker = TableChunker(self.settings, self.estimator)

    def chunk(self, document: NormalizedDocument) -> ChunkedDocument:
        relative_path = str(document.metadata.get("relative_path") or "").replace("\\", "/")
        if not relative_path:
            raise ValueError("NormalizedDocument has no relative_path")
        excluded_pages = self.overrides.pages_for(relative_path)
        asset_ids = {asset.asset_id for asset in document.assets}
        image_links = self._image_links(document, asset_ids)
        ocr_pages = {
            page.page_number
            for page in document.pages
            if page.ocr_status == "completed" and page.metadata.get("ocr_text_for_rag") is not None
        }
        page_by_number = {page.page_number: page for page in document.pages}
        source_elements, derived_sources = self._source_elements(
            document, ocr_pages, excluded_pages
        )
        skipped: Counter[str] = Counter()
        eligible_ids: set[str] = set()
        candidates: list[ChunkCandidate] = []
        heading_stack: list[HeadingState] = []
        buffer: list[SourceUnit] = []
        derived_native_mode = False
        last_derived_page: int | None = None

        def flush_text() -> None:
            nonlocal buffer
            if not buffer:
                return
            candidate = self._text_candidate(buffer, image_links, page_by_number)
            if candidate is not None:
                candidates.append(candidate)
            buffer = []

        for element in source_elements:
            is_derived_native = bool(element.metadata.get("derived_source"))
            if is_derived_native:
                page_gap = bool(
                    last_derived_page is not None
                    and element.page_number is not None
                    and element.page_number != last_derived_page + 1
                )
                if not derived_native_mode or page_gap:
                    flush_text()
                    heading_stack = []
                derived_native_mode = True
                last_derived_page = element.page_number
            else:
                derived_native_mode = False
                last_derived_page = None
            if element.page_number in excluded_pages:
                flush_text()
                heading_stack = []
                skipped["explicitly_excluded_page"] += 1
                continue
            if element.type == "image":
                searchable_title = self._clean_text(
                    str(element.metadata.get("searchable_title") or "")
                )
                asset_id = str(element.metadata.get("asset_id") or "")
                if not searchable_title or asset_id not in asset_ids:
                    skipped["non_searchable_type"] += 1
                    continue
                flush_text()
                eligible_ids.add(element.element_id)
                candidates.append(
                    self._standalone_visual_candidate(
                        element,
                        searchable_title,
                        asset_id,
                        relative_path,
                    )
                )
                continue
            if element.type not in SEARCHABLE_TYPES:
                skipped["non_searchable_type"] += 1
                continue
            if element.page_number in ocr_pages and not element.metadata.get("ocr_managed"):
                skipped["native_superseded_by_completed_ocr"] += 1
                continue
            if element.metadata.get("exclude_from_rag_text"):
                skipped["excluded_header_or_footer"] += 1
                continue
            source_type = "ocr" if element.metadata.get("ocr_managed") else "native"
            if element.type == "table":
                parsed_table = self.table_chunker.parse(element)
                if parsed_table is None:
                    skipped["empty_or_unrecoverable_table"] += 1
                    continue
                table_parts = self.table_chunker.split(parsed_table)
                if not table_parts:
                    skipped["empty_or_unrecoverable_table"] += 1
                    continue
                eligible_ids.add(element.element_id)
                if any(unit.kind != "heading" for unit in buffer):
                    flush_text()
                table_heading_units = (
                    list(buffer) if buffer else self._context_units(heading_stack)
                )
                buffer = []
                heading_path, heading_ids, parent_section_id = self._heading_context(heading_stack)
                context_pages = [
                    item.element.page_number
                    for item in table_heading_units
                    if item.element.page_number is not None
                ]
                for table_part in table_parts:
                    table_heading_ids = [item.element.element_id for item in table_heading_units]
                    source_ids = self._unique([*table_heading_ids, element.element_id])
                    eligible_ids.update(table_heading_ids)
                    candidate = self._table_candidate(
                        element,
                        table_part,
                        heading_path,
                        parent_section_id,
                        source_ids,
                        self._source_type({source_type, *(item.source_type for item in table_heading_units)}),
                        image_links,
                        page_by_number,
                        parsed_table.metadata,
                        context_pages,
                        self._unique([item.text for item in table_heading_units]),
                    )
                    candidates.append(candidate)
                continue

            text = self._clean_text(element.text or "")
            if not text:
                skipped["empty_text_element"] += 1
                continue
            eligible_ids.add(element.element_id)
            if element.type == "heading":
                if any(unit.kind != "heading" for unit in buffer):
                    flush_text()
                level = self._heading_level(text, heading_stack, element)
                while heading_stack and heading_stack[-1].level >= level:
                    heading_stack.pop()
                heading_stack.append(HeadingState(level, text, element, source_type))
                heading_path, heading_ids, parent_section_id = self._heading_context(heading_stack)
                unit = SourceUnit(
                    element=element,
                    text=text,
                    kind="heading",
                    heading_path=heading_path,
                    heading_ids=heading_ids,
                    parent_section_id=parent_section_id,
                    source_type=source_type,
                )
                if buffer and self.estimator.count(self._render_units([*buffer, unit])) > self.settings.hard_max_tokens:
                    flush_text()
                buffer.append(unit)
                continue

            if not buffer and heading_stack:
                buffer.extend(self._context_units(heading_stack))
            heading_path, heading_ids, parent_section_id = self._heading_context(heading_stack)
            context_tokens = self.estimator.count(self._render_units(buffer)) if buffer else 0
            split_limit = max(32, self.settings.hard_max_tokens - context_tokens - 12)
            pieces = self.estimator.split_text(text, split_limit)
            for part_index, piece in enumerate(pieces):
                unit = SourceUnit(
                    element=element,
                    text=piece,
                    kind=element.type,
                    heading_path=heading_path,
                    heading_ids=heading_ids,
                    parent_section_id=parent_section_id,
                    source_type=source_type,
                    split_part=part_index if len(pieces) > 1 else None,
                )
                candidate_text = self._render_units([*buffer, unit])
                if buffer and any(item.kind != "heading" for item in buffer) and self.estimator.count(candidate_text) > self.settings.hard_max_tokens:
                    flush_text()
                    if heading_stack:
                        buffer.extend(self._context_units(heading_stack))
                buffer.append(unit)
                if self.estimator.count(self._render_units(buffer)) >= self.settings.target_tokens:
                    flush_text()
        flush_text()

        candidates = self._coalesce_weak_small_text_candidates(candidates)
        chunks = self._materialize(document, relative_path, candidates)
        covered_ids = {element_id for chunk in chunks for element_id in chunk.source_element_ids}
        missing = sorted(eligible_ids - covered_ids)
        if missing:
            raise RuntimeError(
                f"chunk coverage failure for {relative_path}: {len(missing)} source elements missing: "
                + ", ".join(missing[:10])
            )
        return ChunkedDocument(
            document_id=document.document_id,
            version_id=document.version_id,
            relative_path=relative_path,
            source_sha256=str(document.metadata.get("sha256") or "") or None,
            chunker_version=self.settings.chunker_version,
            settings_hash=self.effective_settings_hash,
            tokenizer_method=self.estimator.method,
            chunks=chunks,
            coverage=ChunkCoverage(
                eligible_source_elements=len(eligible_ids),
                covered_source_elements=len(eligible_ids - set(missing)),
                missing_source_element_ids=missing,
                skipped_source_elements=dict(sorted(skipped.items())),
            ),
            metadata={
                "normalized_schema_version": document.schema_version,
                "source_element_count": len(document.elements),
                "source_asset_count": len(document.assets),
                "ocr_completed_pages": sorted(ocr_pages),
                "explicitly_excluded_pages": sorted(excluded_pages),
                "override_notes": self.overrides.notes,
                "derived_native_page_sources": derived_sources,
            },
        )

    def _standalone_visual_candidate(
        self,
        element: NormalizedElement,
        title: str,
        asset_id: str,
        relative_path: str,
    ) -> ChunkCandidate:
        parent_path = relative_path.rsplit("/", 1)[0] if "/" in relative_path else ""
        text = f"Изображение: {title}"
        if parent_path:
            text += f"\nРаздел базы знаний: {parent_path}"
        return ChunkCandidate(
            chunk_type="text",
            text=text,
            token_count=self.estimator.count(text),
            page_start=None,
            page_end=None,
            heading_path=[title],
            section_title=title,
            parent_section_id=None,
            source_element_ids=[element.element_id],
            related_image_ids=[asset_id],
            related_table_ids=[],
            source_type="native",
            metadata={
                "element_types": {"image": 1},
                "token_estimator": self.estimator.method,
                "source_fallback": "standalone_visual_filename",
                "source_fallback_reason": "standalone_visual_has_no_searchable_text",
                "visual_content_status": "not_ocr_processed",
            },
        )

    def _source_elements(
        self,
        document: NormalizedDocument,
        ocr_pages: set[int],
        excluded_pages: set[int],
    ) -> tuple[list[NormalizedElement], list[dict[str, Any]]]:
        """Fill structural gaps from page.native_text without mutating NormalizedDocument."""
        covered_pages: set[int] = set()
        for element in document.elements:
            if element.page_number is None or element.type not in SEARCHABLE_TYPES:
                continue
            if element.metadata.get("exclude_from_rag_text"):
                continue
            has_content = bool(self._clean_text(element.text or ""))
            if element.type == "table":
                has_content = self.table_chunker.parse(element) is not None
            if has_content:
                covered_pages.add(element.page_number)

        namespace = self._namespace(document.document_id)
        derived: list[NormalizedElement] = []
        registry: list[dict[str, Any]] = []
        base_order = max((element.order for element in document.elements), default=-1) + 1
        for page in sorted(document.pages, key=lambda item: item.page_number):
            page_number = page.page_number
            text = self._clean_text(page.native_text or "")
            if (
                not text
                or page_number in covered_pages
                or page_number in ocr_pages
                or page_number in excluded_pages
            ):
                continue
            source_id = str(
                uuid.uuid5(
                    namespace,
                    f"{document.version_id}:normalized-page-native-text:{page_number}",
                )
            )
            element = NormalizedElement(
                element_id=source_id,
                type="paragraph",
                text=text,
                page_number=page_number,
                order=base_order + page_number,
                reading_order=base_order + page_number,
                metadata={
                    "derived_source": True,
                    "derived_from": "normalized_page.native_text",
                },
            )
            derived.append(element)
            registry.append(
                {
                    "element_id": source_id,
                    "page_number": page_number,
                    "source": "normalized_page.native_text",
                    "source_fallback": "native_page_text",
                    "fallback_reason": "docling_structural_elements_missing_for_page",
                    "character_count": len(text),
                }
            )

        elements = [*document.elements, *derived]
        if derived:
            elements.sort(
                key=lambda item: (
                    item.page_number is None,
                    item.page_number if item.page_number is not None else 0,
                    item.reading_order if item.reading_order is not None else item.order,
                )
            )
        else:
            elements.sort(key=lambda item: (item.order, item.reading_order or 0))
        return elements, registry

    def _text_candidate(
        self,
        units: list[SourceUnit],
        image_links: dict[str, set[str]],
        page_by_number: dict[int, Any],
    ) -> ChunkCandidate | None:
        text = self._render_units(units).strip()
        if not text:
            return None
        source_ids = self._unique([unit.element.element_id for unit in units])
        pages = sorted(
            {unit.element.page_number for unit in units if unit.element.page_number is not None}
        )
        heading_path = next(
            (unit.heading_path for unit in reversed(units) if unit.heading_path), []
        )
        parent_section_id = next(
            (unit.parent_section_id for unit in reversed(units) if unit.parent_section_id), None
        )
        source_types = {unit.source_type for unit in units}
        related_images = self._related_images(source_ids, image_links)
        metadata: dict[str, Any] = {
            "element_types": dict(sorted(Counter(unit.kind for unit in units).items())),
            "token_estimator": self.estimator.method,
            "split_source_parts": [
                {"element_id": unit.element.element_id, "part": unit.split_part}
                for unit in units
                if unit.split_part is not None
            ],
        }
        fallback_units = [unit for unit in units if unit.element.metadata.get("derived_source")]
        if fallback_units:
            metadata.update(
                {
                    "source_fallback": "native_page_text",
                    "source_fallback_reason": "docling_structural_elements_missing_for_page",
                    "fallback_source_ids": self._unique(
                        [unit.element.element_id for unit in fallback_units]
                    ),
                    "fallback_page_numbers": sorted(
                        {
                            unit.element.page_number
                            for unit in fallback_units
                            if unit.element.page_number is not None
                        }
                    ),
                }
            )
        semantic_characters = sum(character.isalnum() for character in text)
        semantic_ratio = semantic_characters / max(1, len(text))
        if self.estimator.count(text) < 30 and (
            semantic_characters == 0 or semantic_ratio < 0.25
        ):
            metadata["retrieval_candidate"] = False
            metadata["retrieval_exclusion_reason"] = "low_semantic_signal"
        provenance = self._ocr_provenance(pages, page_by_number)
        if provenance:
            metadata["ocr_provenance"] = provenance
        return ChunkCandidate(
            chunk_type="text",
            text=text,
            token_count=self.estimator.count(text),
            page_start=pages[0] if pages else None,
            page_end=pages[-1] if pages else None,
            heading_path=list(heading_path),
            section_title=heading_path[-1] if heading_path else None,
            parent_section_id=parent_section_id,
            source_element_ids=source_ids,
            related_image_ids=related_images,
            related_table_ids=[],
            source_type=self._source_type(source_types),
            metadata=metadata,
        )

    def _table_candidate(
        self,
        element: NormalizedElement,
        part: TablePart,
        heading_path: list[str],
        parent_section_id: str | None,
        source_ids: list[str],
        source_type: str,
        image_links: dict[str, set[str]],
        page_by_number: dict[int, Any],
        parsed_metadata: dict[str, Any],
        context_pages: list[int],
        heading_texts: list[str],
    ) -> ChunkCandidate:
        pages = sorted(
            {
                *context_pages,
                *([element.page_number] if element.page_number is not None else []),
            }
        )
        text = "\n".join([*heading_texts, part.text]).strip()
        metadata: dict[str, Any] = {
            "token_estimator": self.estimator.method,
            "table_source_format": part.source_format,
            "table_headers": part.headers,
            "table_rows": part.rows,
            "table_html": part.html,
            "original_table_html": part.original_html,
            "row_start": part.row_start,
            "row_end": part.row_end,
            **parsed_metadata,
        }
        if part.source_format == "table_title_only":
            metadata["retrieval_candidate"] = False
            metadata["retrieval_exclusion_reason"] = "empty_table_or_sheet_title_only"
        if part.hard_max_exception:
            metadata["hard_max_exception"] = part.hard_max_exception
        elif self.estimator.count(text) > self.settings.hard_max_tokens:
            metadata["hard_max_exception"] = "table_heading_context_causes_hard_max"
        provenance = self._ocr_provenance(pages, page_by_number)
        if provenance:
            metadata["ocr_provenance"] = provenance
        return ChunkCandidate(
            chunk_type="table",
            text=text,
            token_count=self.estimator.count(text),
            page_start=pages[0] if pages else None,
            page_end=pages[-1] if pages else None,
            heading_path=list(heading_path),
            section_title=heading_path[-1] if heading_path else None,
            parent_section_id=parent_section_id,
            source_element_ids=source_ids,
            related_image_ids=self._related_images(source_ids, image_links),
            related_table_ids=[element.element_id],
            source_type=source_type,
            metadata=metadata,
        )

    def _coalesce_weak_small_text_candidates(
        self, candidates: list[ChunkCandidate]
    ) -> list[ChunkCandidate]:
        output: list[ChunkCandidate] = []
        index = 0
        while index < len(candidates):
            candidate = candidates[index]
            if not self._weak_small_text(candidate):
                output.append(candidate)
                index += 1
                continue
            element_types = set((candidate.metadata.get("element_types") or {}).keys())
            prefer_next = element_types == {"heading"} or not output
            following = candidates[index + 1] if index + 1 < len(candidates) else None
            if prefer_next and following is not None and self._can_merge_text(candidate, following):
                output.append(self._merge_text_candidates(candidate, following))
                index += 2
                continue
            if output and self._can_merge_text(output[-1], candidate):
                output[-1] = self._merge_text_candidates(output[-1], candidate)
                index += 1
                continue
            if following is not None and self._can_merge_text(candidate, following):
                output.append(self._merge_text_candidates(candidate, following))
                index += 2
                continue
            output.append(candidate)
            index += 1
        return output

    @staticmethod
    def _weak_small_text(candidate: ChunkCandidate) -> bool:
        if candidate.chunk_type != "text" or candidate.token_count >= 30:
            return False
        if candidate.metadata.get("source_fallback"):
            return False
        if candidate.metadata.get("retrieval_candidate") is False:
            return False
        element_types = set((candidate.metadata.get("element_types") or {}).keys())
        return not candidate.heading_path or element_types == {"heading"}

    def _can_merge_text(self, left: ChunkCandidate, right: ChunkCandidate) -> bool:
        if left.chunk_type != "text" or right.chunk_type != "text":
            return False
        if left.source_type != right.source_type:
            return False
        if bool(left.metadata.get("source_fallback")) != bool(
            right.metadata.get("source_fallback")
        ):
            return False
        if left.heading_path and right.heading_path and left.heading_path != right.heading_path:
            return False
        if left.page_end is not None and right.page_start is not None:
            if not 0 <= right.page_start - left.page_end <= 1:
                return False
        return self.estimator.count(f"{left.text}\n\n{right.text}") <= self.settings.hard_max_tokens

    def _merge_text_candidates(
        self, left: ChunkCandidate, right: ChunkCandidate
    ) -> ChunkCandidate:
        text = f"{left.text}\n\n{right.text}".strip()
        element_types = Counter(left.metadata.get("element_types") or {})
        element_types.update(right.metadata.get("element_types") or {})
        metadata = {**left.metadata, **right.metadata}
        metadata["element_types"] = dict(sorted(element_types.items()))
        metadata["coalesced_small_text"] = True
        metadata["coalesced_source_candidate_count"] = int(
            left.metadata.get("coalesced_source_candidate_count", 1)
        ) + int(right.metadata.get("coalesced_source_candidate_count", 1))
        metadata["split_source_parts"] = [
            *(left.metadata.get("split_source_parts") or []),
            *(right.metadata.get("split_source_parts") or []),
        ]
        if left.metadata.get("ocr_provenance") or right.metadata.get("ocr_provenance"):
            provenance = [
                *(left.metadata.get("ocr_provenance") or []),
                *(right.metadata.get("ocr_provenance") or []),
            ]
            metadata["ocr_provenance"] = list(
                {json.dumps(item, sort_keys=True): item for item in provenance}.values()
            )
        pages = [
            page
            for page in (left.page_start, left.page_end, right.page_start, right.page_end)
            if page is not None
        ]
        heading_path = right.heading_path or left.heading_path
        return ChunkCandidate(
            chunk_type="text",
            text=text,
            token_count=self.estimator.count(text),
            page_start=min(pages) if pages else None,
            page_end=max(pages) if pages else None,
            heading_path=list(heading_path),
            section_title=(heading_path[-1] if heading_path else right.section_title or left.section_title),
            parent_section_id=right.parent_section_id or left.parent_section_id,
            source_element_ids=self._unique(
                [*left.source_element_ids, *right.source_element_ids]
            ),
            related_image_ids=self._unique(
                [*left.related_image_ids, *right.related_image_ids]
            ),
            related_table_ids=[],
            source_type=left.source_type,
            metadata=metadata,
        )

    def _materialize(
        self,
        document: NormalizedDocument,
        relative_path: str,
        candidates: list[ChunkCandidate],
    ) -> list[RetrievalChunk]:
        namespace = self._namespace(document.document_id)
        settings_hash = self.effective_settings_hash
        chunk_ids: list[str] = []
        for index, candidate in enumerate(candidates):
            content_hash = hashlib.sha256(candidate.text.encode("utf-8")).hexdigest()
            identity = "|".join(
                [
                    document.version_id,
                    settings_hash,
                    str(index),
                    candidate.chunk_type,
                    ",".join(candidate.source_element_ids),
                    content_hash,
                ]
            )
            chunk_ids.append(str(uuid.uuid5(namespace, identity)))
        chunks: list[RetrievalChunk] = []
        for index, (candidate, chunk_id) in enumerate(zip(candidates, chunk_ids, strict=True)):
            chunks.append(
                RetrievalChunk(
                    chunk_id=chunk_id,
                    document_id=document.document_id,
                    version_id=document.version_id,
                    relative_path=relative_path,
                    chunk_type=candidate.chunk_type,  # type: ignore[arg-type]
                    text=candidate.text,
                    token_count=candidate.token_count,
                    page_start=candidate.page_start,
                    page_end=candidate.page_end,
                    heading_path=candidate.heading_path,
                    section_title=candidate.section_title,
                    parent_section_id=candidate.parent_section_id,
                    source_element_ids=candidate.source_element_ids,
                    previous_chunk_id=chunk_ids[index - 1] if index else None,
                    next_chunk_id=chunk_ids[index + 1] if index + 1 < len(chunk_ids) else None,
                    related_image_ids=candidate.related_image_ids,
                    related_table_ids=candidate.related_table_ids,
                    source_type=candidate.source_type,  # type: ignore[arg-type]
                    metadata=candidate.metadata,
                )
            )
        return chunks

    def _render_units(self, units: list[SourceUnit]) -> str:
        output = ""
        previous: SourceUnit | None = None
        for unit in units:
            value = unit.text.strip()
            if not value:
                continue
            if unit.kind == "heading":
                separator = "\n\n" if output else ""
                rendered = value
            elif unit.kind == "list":
                separator = "\n" if output else ""
                rendered = value if value.startswith(("- ", "• ")) else f"- {value}"
            else:
                continuation = bool(
                    previous
                    and previous.kind == "paragraph"
                    and unit.kind == "paragraph"
                    and previous.element.page_number != unit.element.page_number
                    and not previous.text.rstrip().endswith((".", "!", "?", "…", ":", ";"))
                    and value[:1].islower()
                )
                separator = " " if continuation else "\n\n" if output else ""
                rendered = value
            output += separator + rendered
            previous = unit
        return output.strip()

    @staticmethod
    def _clean_text(text: str) -> str:
        value = CONTROL_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
        lines = [" ".join(line.split()) for line in value.splitlines()]
        return "\n".join(line for line in lines if line).strip()

    @staticmethod
    def _heading_level(
        text: str, stack: list[HeadingState], element: NormalizedElement
    ) -> int:
        match = NUMBERED_HEADING_RE.match(text)
        if match:
            return match.group(1).rstrip(".").count(".") + 1
        if element.metadata.get("ocr_block_type") == "Title":
            return 1
        if not stack:
            return 1
        return stack[-1].level

    @staticmethod
    def _heading_context(
        stack: list[HeadingState],
    ) -> tuple[list[str], list[str], str | None]:
        return (
            [item.text for item in stack],
            [item.element.element_id for item in stack],
            stack[-1].element.element_id if stack else None,
        )

    @staticmethod
    def _context_units(stack: list[HeadingState]) -> list[SourceUnit]:
        heading_path = [item.text for item in stack]
        heading_ids = [item.element.element_id for item in stack]
        parent = stack[-1].element.element_id if stack else None
        return [
            SourceUnit(
                element=item.element,
                text=item.text,
                kind="heading",
                heading_path=heading_path,
                heading_ids=heading_ids,
                parent_section_id=parent,
                source_type=item.source_type,
            )
            for item in stack
        ]

    @staticmethod
    def _image_links(
        document: NormalizedDocument, asset_ids: set[str]
    ) -> dict[str, set[str]]:
        links: dict[str, set[str]] = defaultdict(set)
        for relation in document.relations:
            if relation.type != "related_to":
                continue
            if relation.target_id in asset_ids:
                links[relation.source_id].add(relation.target_id)
            if relation.source_id in asset_ids:
                links[relation.target_id].add(relation.source_id)
        for asset in document.assets:
            for element_id in asset.nearby_text_element_ids:
                links[element_id].add(asset.asset_id)
            if asset.previous_text_element_id:
                links[asset.previous_text_element_id].add(asset.asset_id)
            if asset.next_text_element_id:
                links[asset.next_text_element_id].add(asset.asset_id)
        return links

    @staticmethod
    def _related_images(
        source_ids: list[str], image_links: dict[str, set[str]]
    ) -> list[str]:
        return sorted({asset for source_id in source_ids for asset in image_links.get(source_id, set())})

    @staticmethod
    def _source_type(values: set[str]) -> str:
        if values == {"ocr"}:
            return "ocr"
        if values == {"native"}:
            return "native"
        return "mixed"

    @staticmethod
    def _ocr_provenance(pages: list[int], page_by_number: dict[int, Any]) -> list[dict[str, Any]]:
        provenance: list[dict[str, Any]] = []
        for page_number in pages:
            page = page_by_number.get(page_number)
            if page is None or page.ocr_status != "completed" or not page.ocr_provenance:
                continue
            provenance.append({"page_number": page_number, **page.ocr_provenance})
        return provenance

    @staticmethod
    def _namespace(document_id: str) -> uuid.UUID:
        try:
            return uuid.UUID(document_id)
        except ValueError:
            return uuid.uuid5(uuid.NAMESPACE_URL, document_id)

    @staticmethod
    def _unique(values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))
