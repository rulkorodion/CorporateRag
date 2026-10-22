from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import PROJECT_ROOT


class APISettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    host: str = Field(default="127.0.0.1", validation_alias="API_HOST")
    port: int = Field(default=8000, ge=1, le=65535, validation_alias="API_PORT")
    embedding_concurrency: int = Field(
        default=1, ge=1, validation_alias="API_EMBEDDING_CONCURRENCY"
    )
    reranker_concurrency: int = Field(
        default=1, ge=1, validation_alias="API_RERANKER_CONCURRENCY"
    )
    llm_concurrency: int = Field(
        default=1, ge=1, validation_alias="API_LLM_CONCURRENCY"
    )
    ollama_num_parallel: int = Field(
        default=1, ge=1, validation_alias="OLLAMA_NUM_PARALLEL"
    )
    default_max_context_tokens: int = Field(
        default=6000,
        ge=500,
        le=32000,
        validation_alias="API_DEFAULT_MAX_CONTEXT_TOKENS",
    )
    default_max_chunks: int = Field(
        default=8, ge=1, le=20, validation_alias="API_DEFAULT_MAX_CHUNKS"
    )
    max_queue_size: int = Field(
        default=32, ge=0, validation_alias="API_MAX_QUEUE_SIZE"
    )
    queue_wait_timeout_seconds: float = Field(
        default=300.0, gt=0, validation_alias="API_QUEUE_WAIT_TIMEOUT_SECONDS"
    )
    metrics_window_size: int = Field(
        default=1000, ge=10, validation_alias="API_METRICS_WINDOW_SIZE"
    )
    telemetry_storage_enabled: bool = Field(
        default=False, validation_alias="API_TELEMETRY_STORAGE_ENABLED"
    )
    telemetry_database_path: Path = Field(
        default=PROJECT_ROOT / "storage" / "telemetry" / "requests.db",
        validation_alias="API_TELEMETRY_DATABASE_PATH",
    )
    telemetry_retention_days: int = Field(
        default=90,
        ge=7,
        le=3650,
        validation_alias="API_TELEMETRY_RETENTION_DAYS",
    )
    warmup_on_startup: bool = Field(
        default=True, validation_alias="API_WARMUP_ON_STARTUP"
    )
    warmup_retry_initial_seconds: float = Field(
        default=5.0,
        ge=0.1,
        le=300.0,
        validation_alias="API_WARMUP_RETRY_INITIAL_SECONDS",
    )
    warmup_retry_max_seconds: float = Field(
        default=60.0,
        ge=0.1,
        le=900.0,
        validation_alias="API_WARMUP_RETRY_MAX_SECONDS",
    )
    cors_allowed_origins: str = Field(
        default="", validation_alias="API_CORS_ALLOWED_ORIGINS"
    )
    onlyoffice_public_url: str = Field(
        default="http://127.0.0.1:8082",
        validation_alias="ONLYOFFICE_PUBLIC_URL",
    )
    onlyoffice_internal_file_base_url: str = Field(
        default="http://host.docker.internal:3001",
        validation_alias="ONLYOFFICE_INTERNAL_FILE_BASE_URL",
    )
    onlyoffice_jwt_secret: str = Field(default="", validation_alias="JWT_SECRET")

    @field_validator("cors_allowed_origins")
    @classmethod
    def reject_wildcard_cors(cls, value: str) -> str:
        origins = [item.strip() for item in value.split(",") if item.strip()]
        if "*" in origins:
            raise ValueError("API_CORS_ALLOWED_ORIGINS must not contain '*'")
        return ",".join(origin.rstrip("/") for origin in origins)

    @field_validator("onlyoffice_public_url", "onlyoffice_internal_file_base_url")
    @classmethod
    def normalize_onlyoffice_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value.startswith("/") and not value.startswith("//"):
            return value
        if not value.startswith(("http://", "https://")):
            raise ValueError(
                "ONLYOFFICE URL must be an absolute http(s) URL or a root-relative path"
            )
        return value

    @model_validator(mode="after")
    def validate_llm_parallelism(self) -> "APISettings":
        if self.llm_concurrency > self.ollama_num_parallel:
            raise ValueError(
                "API_LLM_CONCURRENCY cannot exceed OLLAMA_NUM_PARALLEL"
            )
        if self.warmup_retry_max_seconds < self.warmup_retry_initial_seconds:
            raise ValueError(
                "API_WARMUP_RETRY_MAX_SECONDS cannot be less than "
                "API_WARMUP_RETRY_INITIAL_SECONDS"
            )
        return self

    @property
    def allowed_origins(self) -> list[str]:
        return [
            origin
            for origin in self.cors_allowed_origins.split(",")
            if origin
        ]
