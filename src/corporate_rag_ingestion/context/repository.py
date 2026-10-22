from __future__ import annotations

from pathlib import Path

from ..chunking.models import ChunkedDocument, RetrievalChunk
from ..schemas import NormalizedDocument
from .models import RelatedImage


class ChunkRepository:
    """Read-only index over the versioned chunk files."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._chunks: dict[str, RetrievalChunk] | None = None

    def _load(self) -> dict[str, RetrievalChunk]:
        if self._chunks is None:
            chunks: dict[str, RetrievalChunk] = {}
            for path in sorted(self.root.rglob("*.json")):
                document = ChunkedDocument.model_validate_json(path.read_text(encoding="utf-8"))
                for chunk in document.chunks:
                    if chunk.chunk_id in chunks:
                        raise ValueError(f"duplicate chunk ID in storage: {chunk.chunk_id}")
                    chunks[chunk.chunk_id] = chunk
            self._chunks = chunks
        return self._chunks

    def get(self, chunk_id: str) -> RetrievalChunk | None:
        return self._load().get(chunk_id)

    def table_header_candidates(
        self,
        document_id: str,
        version_id: str,
        related_table_ids: list[str],
    ) -> list[list[str]]:
        table_ids = set(related_table_ids)
        candidates: list[list[str]] = []
        for chunk in self._load().values():
            if chunk.document_id != document_id or chunk.version_id != version_id:
                continue
            if chunk.chunk_type != "table":
                continue
            if table_ids and not table_ids.intersection(chunk.related_table_ids):
                continue
            for row in chunk.metadata.get("table_rows") or []:
                candidates.append([str(value) for value in row])
        return candidates

    def all(self) -> list[RetrievalChunk]:
        return list(self._load().values())


class AssetRepository:
    """Resolves only assets present in NormalizedDocument and on local storage."""

    def __init__(self, normalized_root: Path, knowledge_base_root: Path | None = None) -> None:
        self.normalized_root = normalized_root.resolve()
        self.storage_root = self.normalized_root.parent
        self.knowledge_base_root = (
            knowledge_base_root.resolve() if knowledge_base_root is not None else None
        )
        self._documents: dict[tuple[str, str], NormalizedDocument | None] = {}

    def _document(self, document_id: str, version_id: str) -> NormalizedDocument | None:
        key = (document_id, version_id)
        if key not in self._documents:
            path = self.normalized_root / document_id / f"{version_id}.json"
            self._documents[key] = (
                NormalizedDocument.model_validate_json(path.read_text(encoding="utf-8"))
                if path.is_file()
                else None
            )
        return self._documents[key]

    def resolve(
        self, document_id: str, version_id: str, asset_ids: list[str]
    ) -> tuple[list[RelatedImage], list[str]]:
        document = self._document(document_id, version_id)
        if document is None:
            return [], list(dict.fromkeys(asset_ids))
        assets = {asset.asset_id: asset for asset in document.assets}
        found: list[RelatedImage] = []
        missing: list[str] = []
        for asset_id in dict.fromkeys(asset_ids):
            asset = assets.get(asset_id)
            if asset is None:
                missing.append(asset_id)
                continue
            path_scope = str(asset.metadata.get("path_scope") or "storage")
            root = (
                self.knowledge_base_root
                if path_scope == "knowledge_base" and self.knowledge_base_root is not None
                else self.storage_root
            )
            local_path = (root / asset.local_path).resolve()
            if not local_path.is_relative_to(root) or not local_path.is_file():
                missing.append(asset_id)
                continue
            found.append(
                RelatedImage(
                    asset_id=asset.asset_id,
                    document_id=document_id,
                    version_id=version_id,
                    local_path=asset.local_path,
                    source_page=asset.source_page,
                    visual_role=asset.visual_role,
                    visual_classification_status=asset.visual_classification_status,
                    path_scope=("knowledge_base" if root == self.knowledge_base_root else "storage"),
                )
            )
        return found, missing
