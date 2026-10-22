from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ChunkingOverrides:
    excluded_pages: dict[str, set[int]] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "ChunkingOverrides":
        if not path.is_file():
            return cls()
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        raw_pages = payload.get("excluded_pages") or {}
        excluded = {
            str(relative_path).replace("\\", "/"): {int(page) for page in pages}
            for relative_path, pages in raw_pages.items()
        }
        return cls(excluded_pages=excluded, notes=dict(payload.get("notes") or {}))

    def pages_for(self, relative_path: str) -> set[int]:
        return self.excluded_pages.get(relative_path.replace("\\", "/"), set())

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "excluded_pages": {
                path: sorted(pages) for path, pages in sorted(self.excluded_pages.items())
            },
            "notes": self.notes,
        }

    def settings_hash(self, base_settings: dict[str, Any]) -> str:
        payload = {
            "chunking": base_settings,
            "overrides": self.canonical_payload(),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
