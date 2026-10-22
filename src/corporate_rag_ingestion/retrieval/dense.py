from __future__ import annotations

import gc
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np

from .models import EmbeddingRuntime
from .settings import RetrievalSettings


LOGGER = logging.getLogger(__name__)


class DenseEmbeddingBackend(Protocol):
    @property
    def runtime(self) -> EmbeddingRuntime: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def token_counts(self, texts: Sequence[str]) -> list[int]: ...


class QwenDenseEmbedder:
    """Lazy, single-load SentenceTransformers wrapper for Qwen3 Embedding."""

    def __init__(self, settings: RetrievalSettings) -> None:
        self.settings = settings
        self._model = None
        self._runtime: EmbeddingRuntime | None = None

    def _load(self):  # type: ignore[no-untyped-def]
        if self._model is not None:
            return self._model
        import torch
        from sentence_transformers import SentenceTransformer

        device = self.settings.dense_device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        cache_folder = str(self.settings.model_cache_path)
        Path(cache_folder).mkdir(parents=True, exist_ok=True)
        LOGGER.info(
            "Loading dense embedding model %s revision=%s device=%s",
            self.settings.dense_model_id,
            self.settings.dense_model_revision,
            device,
        )
        model_source = self.settings.dense_model_path or self.settings.dense_model_id
        model_kwargs = {
            "device": device,
            "cache_folder": cache_folder,
            "trust_remote_code": False,
            "local_files_only": self.settings.dense_local_files_only,
            "model_kwargs": {"dtype": self.settings.dense_dtype},
        }
        if self.settings.dense_model_path is None:
            model_kwargs["revision"] = self.settings.dense_model_revision
        model = SentenceTransformer(
            str(model_source),
            **model_kwargs,
        )
        model.max_seq_length = self.settings.dense_max_length
        if hasattr(model, "get_embedding_dimension"):
            dimension = int(model.get_embedding_dimension() or 0)
        else:  # Compatibility with older sentence-transformers releases.
            dimension = int(model.get_sentence_embedding_dimension() or 0)
        if dimension != self.settings.dense_dimension:
            raise RuntimeError(
                f"dense dimension mismatch: configured={self.settings.dense_dimension}, model={dimension}"
            )
        revision = self.settings.dense_model_revision
        dtype = str(next(model.parameters()).dtype).replace("torch.", "")
        self._model = model
        self._runtime = EmbeddingRuntime(
            model_id=self.settings.dense_model_id,
            model_revision=revision,
            dimension=dimension,
            device=str(device),
            dtype=dtype,
            settings_hash=self.settings.dense_settings_hash,
        )
        LOGGER.info("Dense model ready: dimension=%d dtype=%s", dimension, dtype)
        return model

    @property
    def runtime(self) -> EmbeddingRuntime:
        self._load()
        assert self._runtime is not None
        return self._runtime

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        vectors = model.encode(
            list(texts),
            batch_size=self.settings.dense_batch_size,
            normalize_embeddings=True,
            show_progress_bar=True,
            convert_to_numpy=True,
        )
        return np.asarray(vectors, dtype=np.float32).tolist()

    def embed_query(self, text: str) -> list[float]:
        model = self._load()
        prompts = getattr(model, "prompts", {}) or {}
        kwargs = {"prompt_name": "query"} if "query" in prompts else {
            "prompt": self.settings.query_instruction + ": "
        }
        vector = model.encode(
            [text],
            batch_size=1,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
            **kwargs,
        )[0]
        return np.asarray(vector, dtype=np.float32).tolist()

    def token_counts(self, texts: Sequence[str]) -> list[int]:
        tokenizer = self._load().tokenizer
        encoded = tokenizer(
            list(texts),
            add_special_tokens=True,
            truncation=False,
            padding=False,
            return_attention_mask=False,
        )
        return [len(item) for item in encoded["input_ids"]]

    def release(self) -> None:
        if self._model is None:
            return
        self._model = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
