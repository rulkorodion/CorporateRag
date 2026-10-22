from __future__ import annotations

import mimetypes
from pathlib import Path
from pathlib import PurePosixPath

from ..generation.models import UsedSource


SOURCE_FILE_EXTENSIONS = frozenset(
    {".pdf", ".docx", ".doc", ".rtf", ".xlsx", ".jpg", ".jpeg", ".png", ".mp4", ".url"}
)
IMAGE_FILE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})


def safe_path(root: Path, relative_path: str) -> Path | None:
    """Resolve a stored relative path without allowing traversal outside root."""

    root = root.resolve()
    normalized = relative_path.replace("\\", "/").strip("/")
    parts = PurePosixPath(normalized).parts
    if (
        not normalized
        or any(part in {"", ".", ".."} for part in parts)
        or any(":" in part for part in parts)
    ):
        return None
    try:
        candidate = root.joinpath(*parts).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return None
        return candidate
    except (OSError, RuntimeError, ValueError):
        return None


def safe_source_path(root: Path, relative_path: str) -> Path | None:
    candidate = safe_path(root, relative_path)
    if candidate is None or candidate.suffix.casefold() not in SOURCE_FILE_EXTENSIONS:
        return None
    return candidate


def safe_asset_path(
    *, knowledge_base_root: Path, storage_root: Path, relative_path: str, scope: str
) -> Path | None:
    root = knowledge_base_root if scope == "knowledge_base" else storage_root
    candidate = safe_path(root, relative_path)
    if candidate is None or candidate.suffix.casefold() not in IMAGE_FILE_EXTENSIONS:
        return None
    if scope == "storage":
        assets_root = (storage_root / "assets").resolve()
        if not candidate.is_relative_to(assets_root):
            return None
    return candidate


def source_file(root: Path, source: UsedSource) -> Path | None:
    return safe_source_path(root, source.relative_path)


def asset_file(storage_root: Path, local_path: str) -> Path | None:
    return safe_path(storage_root, local_path)


def mime_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def source_location(source: UsedSource) -> str:
    location = source.relative_path
    if source.page_start is not None:
        pages = str(source.page_start)
        if source.page_end is not None and source.page_end != source.page_start:
            pages += f"–{source.page_end}"
        location += f", стр. {pages}"
    if source.section_title:
        location += f" · {source.section_title}"
    return location
