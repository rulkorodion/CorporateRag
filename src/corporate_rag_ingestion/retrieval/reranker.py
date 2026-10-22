from __future__ import annotations

import gc
import logging
from collections.abc import Sequence
from typing import Protocol

import numpy as np

from .models import RerankerRuntime
from .settings import RetrievalSettings


LOGGER = logging.getLogger(__name__)


class RerankerBackend(Protocol):
    @property
    def runtime(self) -> RerankerRuntime: ...

    def score(self, query: str, documents: Sequence[str]) -> list[float]: ...

    def release(self) -> None: ...


class QwenCrossEncoderReranker:
    """Official SentenceTransformers CrossEncoder backend for Qwen3-Reranker."""

    def __init__(self, settings: RetrievalSettings) -> None:
        self.settings = settings
        self._model = None
        self._runtime: RerankerRuntime | None = None

    @staticmethod
    def resolve_device(configured: str, cuda_available: bool) -> str:
        if configured != "auto":
            return configured
        return "cuda" if cuda_available else "cpu"

    def _load(self):  # type: ignore[no-untyped-def]
        if self._model is not None:
            return self._model
        import torch
        from sentence_transformers import CrossEncoder

        device = self.resolve_device(
            self.settings.reranker_device,
            torch.cuda.is_available(),
        )
        self.settings.model_cache_path.mkdir(parents=True, exist_ok=True)
        LOGGER.info(
            "Loading reranker %s revision=%s device=%s",
            self.settings.reranker_model_id,
            self.settings.reranker_model_revision,
            device,
        )
        model_source = self.settings.reranker_model_path or self.settings.reranker_model_id
        model_kwargs = {
            "device": device,
            "cache_folder": str(self.settings.model_cache_path),
            "trust_remote_code": False,
            "local_files_only": self.settings.reranker_local_files_only,
            "max_length": self.settings.reranker_max_length,
            "prompts": {"query": self.settings.reranker_instruction},
            "default_prompt_name": "query",
            "model_kwargs": {"dtype": self.settings.reranker_dtype},
        }
        if self.settings.reranker_model_path is None:
            model_kwargs["revision"] = self.settings.reranker_model_revision
        model = CrossEncoder(
            str(model_source),
            **model_kwargs,
        )
        dtype = "unknown"
        underlying = getattr(model, "model", None)
        if underlying is not None:
            try:
                dtype = str(next(underlying.parameters()).dtype).replace("torch.", "")
            except (StopIteration, AttributeError):
                pass
        self._model = model
        self._runtime = RerankerRuntime(
            model_id=self.settings.reranker_model_id,
            model_revision=self.settings.reranker_model_revision,
            device=device,
            dtype=dtype,
            batch_size=self.settings.reranker_batch_size,
            max_length=self.settings.reranker_max_length,
            settings_hash=self.settings.reranker_settings_hash,
        )
        LOGGER.info("Reranker ready: dtype=%s", dtype)
        return model

    @property
    def runtime(self) -> RerankerRuntime:
        self._load()
        assert self._runtime is not None
        return self._runtime

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        import torch

        pairs = [(query, document) for document in documents]
        if torch.cuda.is_available() and self.runtime.device == "cuda":
            torch.cuda.reset_peak_memory_stats()
        scores = self._load().predict(
            pairs,
            prompt_name="query",
            batch_size=self.settings.reranker_batch_size,
            show_progress_bar=False,
            activation_fn=torch.nn.Sigmoid(),
            convert_to_numpy=True,
        )
        assert self._runtime is not None
        if torch.cuda.is_available() and self._runtime.device == "cuda":
            self._runtime.peak_device_memory_bytes = max(
                self._runtime.peak_device_memory_bytes,
                int(torch.cuda.max_memory_reserved()),
            )
        try:
            import psutil

            memory = psutil.Process().memory_info()
            self._runtime.process_rss_bytes = max(
                self._runtime.process_rss_bytes,
                int(memory.rss),
            )
            self._runtime.process_peak_rss_bytes = max(
                self._runtime.process_peak_rss_bytes,
                int(getattr(memory, "peak_wset", memory.rss)),
            )
        except ImportError:
            pass
        values = np.asarray(scores, dtype=np.float32).reshape(-1)
        return [float(value) for value in values]

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
