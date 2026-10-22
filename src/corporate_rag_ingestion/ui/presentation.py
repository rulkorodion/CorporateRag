from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..generation.models import RAGAnswer, SourceAttachment, UsedSource
from ..schemas import BoundingBox, NormalizedAsset, NormalizedDocument


_CITATION_RE = re.compile(r"\[(\d+)\]")
_WORD_RE = re.compile(r"[\w-]{3,}", re.UNICODE)


@dataclass(frozen=True)
class InlineImage:
    asset_id: str
    source_number: int
    local_path: str
    source_page: int | None
    width: int | None
    height: int | None
    document_relative_path: str
    bbox: BoundingBox | None
    path_scope: str = "storage"


class NormalizedPresentationRepository:
    """Read-only UI projection over the normalized store."""

    def __init__(self, normalized_root: Path) -> None:
        self.root = normalized_root.resolve()
        self._documents: dict[tuple[str, str], NormalizedDocument | None] = {}
        self._latest: dict[str, NormalizedDocument | None] = {}

    def document(self, document_id: str, version_id: str) -> NormalizedDocument | None:
        key = (document_id, version_id)
        if key not in self._documents:
            path = self.root / document_id / f"{version_id}.json"
            self._documents[key] = (
                NormalizedDocument.model_validate_json(path.read_text(encoding="utf-8"))
                if path.is_file()
                else None
            )
        return self._documents[key]

    def latest_document(self, document_id: str) -> NormalizedDocument | None:
        if document_id not in self._latest:
            directory = self.root / document_id
            paths = list(directory.glob("*.json")) if directory.is_dir() else []
            path = max(paths, key=lambda item: item.stat().st_mtime_ns) if paths else None
            self._latest[document_id] = (
                NormalizedDocument.model_validate_json(path.read_text(encoding="utf-8"))
                if path is not None
                else None
            )
        return self._latest[document_id]

    def source_attachment(self, source: UsedSource) -> SourceAttachment:
        indexed = self.document(source.document_id, source.version_id)
        if indexed is not None:
            video_id = next(
                (
                    relation.target_id
                    for relation in indexed.relations
                    if relation.type == "transcript_of"
                    and relation.source_id == source.document_id
                ),
                None,
            )
            video = self.latest_document(video_id) if video_id else None
            video_path = video.metadata.get("relative_path") if video is not None else None
            if isinstance(video_path, str) and video_path:
                return SourceAttachment(
                    source_number=source.source_number,
                    display_relative_path=video_path,
                    download_relative_path=video_path,
                    media_type="video",
                    indexed_relative_path=source.relative_path,
                    indexed_document_id=source.document_id,
                    document_id=video.document_id,
                    version_id=video.version_id,
                )
        return SourceAttachment(
            source_number=source.source_number,
            display_relative_path=source.relative_path,
            download_relative_path=source.relative_path,
            media_type="document",
            indexed_relative_path=source.relative_path,
            indexed_document_id=source.document_id,
            document_id=source.document_id,
            version_id=source.version_id,
        )

    def enrich_attachments(self, answer: RAGAnswer) -> RAGAnswer:
        return answer.model_copy(
            update={
                "source_attachments": [
                    self.source_attachment(source) for source in answer.used_sources
                ]
            }
        )

    def inline_images(
        self,
        answer: RAGAnswer,
        blocks: list[str],
        *,
        max_images_per_source: int = 4,
    ) -> dict[int, list[InlineImage]]:
        """Place only directly retrieved standalone JPG/PNG visual sources."""

        output: dict[int, list[InlineImage]] = defaultdict(list)
        emitted_asset_ids: set[str] = set()
        images_by_id = {image.asset_id: image for image in answer.related_images}
        cited_blocks: dict[int, list[int]] = defaultdict(list)
        for index, block in enumerate(blocks):
            for value in dict.fromkeys(_CITATION_RE.findall(block)):
                cited_blocks[int(value)].append(index)

        standalone_source_numbers = {
            source.source_number
            for source in answer.used_sources
            if source.provenance.get("source_fallback")
            == "standalone_visual_filename"
            and source.relative_path.casefold().endswith((".jpg", ".jpeg", ".png"))
        }

        if not standalone_source_numbers:
            return {}

        for source in answer.used_sources:
            if source.source_number not in standalone_source_numbers:
                continue
            target_blocks = cited_blocks.get(source.source_number)
            if not target_blocks:
                continue
            document = self.document(source.document_id, source.version_id)
            if document is None:
                continue
            elements = {element.element_id: element for element in document.elements}
            assets = {asset.asset_id: asset for asset in document.assets}
            source_ids = set(source.source_element_ids)
            related_by_relation: set[str] = set()
            for relation in document.relations:
                if relation.type != "related_to":
                    continue
                if relation.source_id in source_ids and relation.target_id in assets:
                    related_by_relation.add(relation.target_id)
                elif relation.target_id in source_ids and relation.source_id in assets:
                    related_by_relation.add(relation.source_id)

            candidates: list[tuple[NormalizedAsset, str]] = []
            for asset_id in source.related_image_ids:
                image = images_by_id.get(asset_id)
                asset = assets.get(asset_id)
                if image is None or asset is None:
                    continue
                nearby = set(asset.nearby_text_element_ids)
                if asset.previous_text_element_id:
                    nearby.add(asset.previous_text_element_id)
                if asset.next_text_element_id:
                    nearby.add(asset.next_text_element_id)
                width = _integer(asset.metadata.get("width"))
                height = _integer(asset.metadata.get("height"))
                if _is_tiny_decorative(width, height):
                    continue
                context_text = " ".join(
                    elements[element_id].text or ""
                    for element_id in nearby
                    if element_id in elements
                )
                candidates.append((asset, context_text))

            candidates.sort(
                key=lambda item: (
                    -max(
                        _text_overlap(item[1], blocks[index])
                        for index in target_blocks
                    ),
                    _asset_order(item[0]),
                )
            )
            selected = candidates[:max_images_per_source]
            selected.sort(key=lambda item: _asset_order(item[0]))
            for asset, context_text in selected:
                if asset.asset_id in emitted_asset_ids:
                    continue
                block_index = max(
                    target_blocks,
                    key=lambda index: _text_overlap(context_text, blocks[index]),
                )
                output[block_index].append(
                    InlineImage(
                        asset_id=asset.asset_id,
                        source_number=source.source_number,
                        local_path=asset.local_path,
                        source_page=asset.source_page,
                        width=_integer(asset.metadata.get("width")),
                        height=_integer(asset.metadata.get("height")),
                        document_relative_path=str(
                            document.metadata.get("relative_path", source.relative_path)
                        ),
                        bbox=asset.bbox,
                        path_scope=str(asset.metadata.get("path_scope") or "storage"),
                    )
                )
                emitted_asset_ids.add(asset.asset_id)
        return dict(output)


def answer_blocks(markdown: str) -> list[str]:
    """Split answer into renderable paragraphs/list groups without changing text."""

    blocks = [part.strip() for part in re.split(r"\n\s*\n", markdown) if part.strip()]
    return blocks or [markdown]


def pdf_clip_rect(bbox: BoundingBox, page_height: float) -> tuple[float, float, float, float]:
    """Convert our normalized PDF bbox to PyMuPDF top-left coordinates."""

    x0, x1 = sorted((bbox.left, bbox.right))
    if (bbox.coordinate_origin or "").casefold() == "bottomleft":
        y0, y1 = sorted((page_height - bbox.top, page_height - bbox.bottom))
    else:
        y0, y1 = sorted((bbox.top, bbox.bottom))
    return x0, y0, x1, y1


def _integer(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_tiny_decorative(width: int | None, height: int | None) -> bool:
    if width is None or height is None:
        return False
    aspect = width / max(height, 1)
    return width < 70 or height < 24 or (height <= 80 and aspect >= 2.4)


def _asset_order(asset: NormalizedAsset) -> tuple[int, float, str]:
    order = asset.reading_order
    if order is None:
        order = _integer(asset.metadata.get("order"))
    return (asset.source_page or 0, float(order if order is not None else math.inf), asset.asset_id)


def _text_overlap(left: str, right: str) -> int:
    left_words = {word.casefold() for word in _WORD_RE.findall(left)}
    right_words = {word.casefold() for word in _WORD_RE.findall(right)}
    return len(left_words & right_words)
