from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field


class ContextBuilderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    builder_version: str = "context-v1.2-table-fields-citations"
    max_context_tokens: int = Field(default=6000, gt=0)
    max_chunks: int = Field(default=10, gt=0)
    max_chunks_per_document: int = Field(default=3, gt=0)
    neighbor_expansion_limit: int = Field(default=2, ge=0)
    neighbor_trigger_max_tokens: int = Field(default=220, gt=0)
    neighbor_max_tokens: int = Field(default=500, gt=0)
    near_duplicate_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    near_duplicate_min_words: int = Field(default=12, gt=0)

    @property
    def settings_hash(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
