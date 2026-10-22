from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class MediaOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript_not_required: list[str] = Field(default_factory=list)

    @property
    def normalized_not_required_paths(self) -> set[str]:
        return {Path(path).as_posix().casefold() for path in self.transcript_not_required}


def load_media_overrides(path: Path) -> MediaOverrides:
    if not path.exists():
        return MediaOverrides()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return MediaOverrides.model_validate(payload)
    except Exception as exc:
        raise ValueError(f"Invalid media overrides config {path}: {exc}") from exc
