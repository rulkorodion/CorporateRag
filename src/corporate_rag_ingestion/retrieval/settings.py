from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class RetrievalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RETRIEVAL_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    qdrant_path: Path = Path("storage/qdrant")
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    collection_name: str = "corporate_knowledge_chunks_v1"
    dense_vector_name: str = "dense"
    sparse_vector_name: str = "bm25"
    dense_model_id: str = "Qwen/Qwen3-Embedding-0.6B"
    dense_model_revision: str = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
    dense_model_path: Path | None = None
    dense_dimension: int = Field(default=1024, gt=0)
    dense_batch_size: int = Field(default=4, gt=0)
    dense_max_length: int = Field(default=4096, gt=0)
    dense_device: str = "auto"
    dense_dtype: str = "auto"
    dense_local_files_only: bool = True
    query_instruction: str = (
        "Given a search query, retrieve relevant passages from a Russian corporate knowledge base"
    )
    sparse_model_id: str = "Qdrant/bm25"
    sparse_model_revision: str = "e499a1f8d6bec960aab5533a0941bf914e70faf9"
    sparse_language: str = "russian"
    sparse_local_files_only: bool = True
    sparse_cache_path: Path = Path("storage/models/fastembed")
    model_cache_path: Path = Path("storage/models/huggingface")
    candidate_limit: int = Field(default=50, gt=0)
    rrf_k: int = Field(default=60, gt=0)
    reranker_model_id: str = "Qwen/Qwen3-Reranker-0.6B"
    reranker_model_revision: str = "e61197ed45024b0ed8a2d74b80b4d909f1255473"
    reranker_model_path: Path | None = None
    reranker_batch_size: int = Field(default=2, gt=0)
    reranker_max_length: int = Field(default=2048, gt=0)
    reranker_device: str = "auto"
    reranker_dtype: str = "auto"
    reranker_local_files_only: bool = True
    reranker_candidate_limit: int = Field(default=40, gt=0)
    reranker_final_top_k: int = Field(default=10, gt=0)
    reranker_instruction: str = (
        "Given a search query, retrieve relevant passages from a Russian corporate "
        "knowledge base that answer the query"
    )
    reranker_include_headings: bool = True
    reranker_release_dense_before_load: bool = True
    upsert_batch_size: int = Field(default=16, gt=0)
    index_schema_version: str = "hybrid-v1"

    @field_validator("dense_model_path", "reranker_model_path", mode="before")
    @classmethod
    def normalize_optional_model_path(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def normalize_url(self) -> "RetrievalSettings":
        if self.qdrant_url is not None and not self.qdrant_url.strip():
            self.qdrant_url = None
        return self

    def resolve_paths(self, project_root: Path) -> "RetrievalSettings":
        values = self.model_copy(deep=True)
        for name in (
            "qdrant_path",
            "sparse_cache_path",
            "model_cache_path",
            "dense_model_path",
            "reranker_model_path",
        ):
            path = getattr(values, name)
            if path is None:
                continue
            if not path.is_absolute():
                setattr(values, name, (project_root / path).resolve())
        return values

    def dense_settings(self) -> dict[str, Any]:
        return {
            "model_id": self.dense_model_id,
            "revision": self.dense_model_revision,
            "dimension": self.dense_dimension,
            "max_length": self.dense_max_length,
            "dtype": self.dense_dtype,
            "normalize_embeddings": True,
            "query_instruction": self.query_instruction,
        }

    def sparse_settings(self) -> dict[str, Any]:
        return {
            "model_id": self.sparse_model_id,
            "revision": self.sparse_model_revision,
            "language": self.sparse_language,
            "k": 1.2,
            "b": 0.75,
            "idf_modifier": True,
        }

    def reranker_settings(self) -> dict[str, Any]:
        return {
            "model_id": self.reranker_model_id,
            "revision": self.reranker_model_revision,
            "batch_size": self.reranker_batch_size,
            "max_length": self.reranker_max_length,
            "dtype": self.reranker_dtype,
            "instruction": self.reranker_instruction,
            "include_headings": self.reranker_include_headings,
            "score_activation": "sigmoid",
        }

    @staticmethod
    def hash_settings(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def dense_settings_hash(self) -> str:
        return self.hash_settings(self.dense_settings())

    @property
    def sparse_settings_hash(self) -> str:
        return self.hash_settings(self.sparse_settings())

    @property
    def reranker_settings_hash(self) -> str:
        return self.hash_settings(self.reranker_settings())

    @property
    def index_settings_hash(self) -> str:
        return self.hash_settings(
            {
                "schema": self.index_schema_version,
                "dense": self.dense_settings(),
                "sparse": self.sparse_settings(),
                "dense_vector_name": self.dense_vector_name,
                "sparse_vector_name": self.sparse_vector_name,
            }
        )
