from __future__ import annotations

import hashlib
import json

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import PROJECT_ROOT


class GenerationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        frozen=True,
    )

    base_url: str = Field(
        default="http://127.0.0.1:11435", validation_alias="LLM_BASE_URL"
    )
    model: str = Field(default="qwen3:14b", validation_alias="LLM_MODEL")
    trust_env: bool = Field(default=False, validation_alias="LLM_TRUST_ENV")
    timeout_seconds: float = Field(default=120.0, gt=0, validation_alias="LLM_TIMEOUT_SECONDS")
    temperature: float = Field(default=0.1, ge=0, validation_alias="LLM_TEMPERATURE")
    num_ctx: int = Field(default=12288, gt=0, validation_alias="LLM_NUM_CTX")
    keep_alive: str = Field(default="60m", validation_alias="LLM_KEEP_ALIVE")
    think: bool = Field(default=False, validation_alias="LLM_THINK")
    stream: bool = Field(default=False, validation_alias="LLM_STREAM")
    num_predict: int | None = Field(
        default=None, ge=1, validation_alias="LLM_NUM_PREDICT"
    )
    generation_version: str = Field(
        default="rag-generation-v1.0", validation_alias="LLM_GENERATION_VERSION"
    )
    prompt_version: str = Field(
        default="grounded-citations-v1.0", validation_alias="LLM_PROMPT_VERSION"
    )

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("LLM_BASE_URL must use http:// or https://")
        return value

    @property
    def settings_hash(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={
                "base_url",
                "timeout_seconds",
                "trust_env",
            },
        )
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
