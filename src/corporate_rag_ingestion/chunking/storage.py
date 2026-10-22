from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import ChunkedDocument


class ChunkStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path_for(self, document_id: str, version_id: str) -> Path:
        for value, label in ((document_id, "document_id"), (version_id, "version_id")):
            if not value or value in {".", ".."} or "/" in value or "\\" in value:
                raise ValueError(f"unsafe chunk {label}: {value!r}")
        return self.root / document_id / f"{version_id}.json"

    def save(self, document: ChunkedDocument) -> tuple[Path, bool]:
        target = self.path_for(document.document_id, document.version_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            document.model_dump(mode="json"), ensure_ascii=False, indent=2
        ) + "\n"
        if target.is_file() and target.read_text(encoding="utf-8") == content:
            return target, False
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.stem}-", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        return target, True

    def load(self, document_id: str, version_id: str) -> ChunkedDocument:
        path = self.path_for(document_id, version_id)
        return ChunkedDocument.model_validate_json(path.read_text(encoding="utf-8"))
