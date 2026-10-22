from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime settings; environment values intentionally use stable names."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    knowledge_base_path: Path = Field(
        default=Path("База Знаний"), validation_alias="KNOWLEDGE_BASE_PATH"
    )
    storage_path: Path = Field(default=Path("storage"), validation_alias="STORAGE_PATH")
    database_url: str = Field(
        default="sqlite:///storage/document_registry.db",
        validation_alias="DATABASE_URL",
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    docling_document_timeout_seconds: float = Field(
        default=300.0,
        validation_alias="DOCLING_DOCUMENT_TIMEOUT_SECONDS",
        gt=0,
    )
    docling_artifacts_path: Path | None = Field(
        default=None,
        validation_alias="DOCLING_ARTIFACTS_PATH",
    )
    media_overrides_path: Path = Field(
        default=Path("config/media_overrides.json"),
        validation_alias="MEDIA_OVERRIDES_PATH",
    )
    legacy_cache_path: Path = Field(
        default=Path("storage/cache/legacy"),
        validation_alias="LEGACY_CACHE_PATH",
    )
    libreoffice_path: Path | None = Field(default=None, validation_alias="LIBREOFFICE_PATH")
    legacy_conversion_timeout_seconds: float = Field(
        default=180.0,
        validation_alias="LEGACY_CONVERSION_TIMEOUT_SECONDS",
        gt=0,
    )
    ocr_cache_path: Path = Field(
        default=Path("storage/ocr"), validation_alias="OCR_CACHE_PATH"
    )
    ocr_import_path: Path = Field(
        default=Path("storage/ocr/imported"), validation_alias="OCR_IMPORT_PATH"
    )
    ocr_manifest_path: Path = Field(
        default=Path("storage/ocr/manifest.jsonl"), validation_alias="OCR_MANIFEST_PATH"
    )
    monkeyocr_enabled: bool = Field(default=False, validation_alias="OCR_MONKEYOCR_ENABLED")
    chunk_storage_path: Path = Field(
        default=Path("storage/chunks"), validation_alias="CHUNK_STORAGE_PATH"
    )
    chunk_overrides_path: Path = Field(
        default=Path("config/chunking_overrides.json"),
        validation_alias="CHUNK_OVERRIDES_PATH",
    )

    def resolve_path(self, path: Path) -> Path:
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    @property
    def knowledge_base_root(self) -> Path:
        return self.resolve_path(self.knowledge_base_path)

    @property
    def storage_root(self) -> Path:
        return self.resolve_path(self.storage_path)

    @property
    def normalized_root(self) -> Path:
        return self.storage_root / "normalized"

    @property
    def assets_root(self) -> Path:
        return self.storage_root / "assets"

    @property
    def media_overrides_file(self) -> Path:
        return self.resolve_path(self.media_overrides_path)

    @property
    def docling_artifacts_root(self) -> Path | None:
        if self.docling_artifacts_path is None:
            return None
        return self.resolve_path(self.docling_artifacts_path)

    @property
    def legacy_cache_root(self) -> Path:
        return self.resolve_path(self.legacy_cache_path)

    @property
    def libreoffice_executable(self) -> Path | None:
        if self.libreoffice_path is None:
            return None
        return self.resolve_path(self.libreoffice_path)

    @property
    def ocr_cache_root(self) -> Path:
        return self.resolve_path(self.ocr_cache_path)

    @property
    def ocr_import_root(self) -> Path:
        return self.resolve_path(self.ocr_import_path)

    @property
    def ocr_manifest_file(self) -> Path:
        return self.resolve_path(self.ocr_manifest_path)

    @property
    def chunk_storage_root(self) -> Path:
        return self.resolve_path(self.chunk_storage_path)

    @property
    def chunk_overrides_file(self) -> Path:
        return self.resolve_path(self.chunk_overrides_path)


def configure_logging(level: str) -> None:
    import logging

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
