from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .schemas import NormalizedDocument


class NormalizedStorage:
    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root.resolve()
        self.normalized_root = self.storage_root / "normalized"
        self.assets_root = self.storage_root / "assets"

    def asset_directory(self, document_id: str) -> Path:
        path = self.assets_root / document_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, document: NormalizedDocument) -> Path:
        target_dir = self.normalized_root / document.document_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{document.version_id}.json"
        payload = document.model_dump(mode="json")
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{document.version_id}-", suffix=".tmp", dir=target_dir
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        return target

    def load(self, relative_path: str) -> NormalizedDocument:
        path = (self.storage_root / relative_path).resolve()
        if not path.is_relative_to(self.storage_root):
            raise ValueError(f"Normalized path escapes storage root: {relative_path}")
        return NormalizedDocument.model_validate_json(path.read_text(encoding="utf-8"))

    def relative_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.storage_root).as_posix()
        except ValueError:
            return str(path.resolve())
