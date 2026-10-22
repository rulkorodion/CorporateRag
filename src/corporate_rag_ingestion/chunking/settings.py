from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings


class ChunkingSettings(BaseSettings):
    model_config = ConfigDict(env_prefix="CHUNK_", extra="ignore")

    chunker_version: str = "structural-v1.2"
    target_tokens: int = Field(default=600, gt=0)
    soft_max_tokens: int = Field(default=700, gt=0)
    hard_max_tokens: int = Field(default=900, gt=0)
    minimum_chunk_tokens: int = Field(default=80, ge=0)
    table_header_rows: int = Field(default=1, ge=1, le=5)
    token_estimator_version: str = "unicode-bytes-and-lexemes-v1"

    @model_validator(mode="after")
    def validate_limits(self) -> "ChunkingSettings":
        if not self.target_tokens <= self.soft_max_tokens <= self.hard_max_tokens:
            raise ValueError("chunk token limits must satisfy target <= soft_max <= hard_max")
        return self

    def effective_settings(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def settings_hash(self) -> str:
        payload = json.dumps(
            self.effective_settings(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
