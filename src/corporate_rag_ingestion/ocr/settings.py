from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings


class MonkeyOCRSettings(BaseSettings):
    """Pinned, verified local-safe MonkeyOCR inference configuration."""

    model_config = ConfigDict(env_prefix="MONKEYOCR_", extra="ignore")

    model_id: str = "zenosai/MonkeyOCRv2-B-Parsing"
    revision: str = "de7a993bd0f39a97b122dac767e82ae04935bce4"
    profile: Literal["local_safe"] = "local_safe"
    pdf_render_dpi: int = Field(default=300, gt=0)
    dtype: Literal["auto", "bfloat16", "float16"] = "auto"
    attention_backend: Literal["sdpa"] = "sdpa"
    transformers_use_kv_cache: Literal[True] = True
    do_sample: Literal[False] = False
    max_pixels: int = Field(default=1_003_520, gt=0)
    layout_max_pixels: int = Field(default=1_003_520, gt=0)
    recognition_max_pixels: int = Field(default=1_003_520, gt=0)
    layout_max_tokens: int = Field(default=4096, gt=0)
    element_max_tokens: int = Field(default=5000, gt=0)
    # Keep the scalar types identical to ``rag_ocr_once.py``'s YAML-derived
    # settings.  JSON canonicalization distinguishes 300 from 300.0, and the
    # hash is the compatibility contract for imported server results.
    layout_max_seconds: int = Field(default=300, gt=0)
    element_max_seconds: int = Field(default=180, gt=0)
    coordinate_scale: int = Field(default=1000, gt=0)
    crop_padding_ratio: float = Field(default=0.0025, ge=0)
    retry_truncated_element: Literal[True] = True
    local_files_only: Literal[True] = True
    prompt_version: Literal[2] = 2
    adapter_version: Literal[4] = 4
    pipeline_version: Literal["layout-crops-v1"] = "layout-crops-v1"
    parsing_mode: Literal["layout_then_original_crops"] = "layout_then_original_crops"
    device: str = "auto"
    cache_dir: Path | None = None
    modules_cache_dir: Path = Path("storage/cache/monkeyocr/huggingface_modules")
    element_cache_dir: Path = Path("storage/cache/monkeyocr/elements")
    model_call_journal: Path = Path("storage/logs/monkeyocr_model_calls.jsonl")

    def effective_inference_settings(self) -> dict[str, Any]:
        monkeyocr = {
            "model_id": self.model_id,
            "revision": self.revision,
            "profile": self.profile,
            "dtype": self.dtype,
            "attention_backend": self.attention_backend,
            "max_pixels": self.max_pixels,
            "layout_max_pixels": self.layout_max_pixels,
            "recognition_max_pixels": self.recognition_max_pixels,
            "transformers_use_kv_cache": self.transformers_use_kv_cache,
            "do_sample": self.do_sample,
            "layout_max_tokens": self.layout_max_tokens,
            "element_max_tokens": self.element_max_tokens,
            "layout_max_seconds": self.layout_max_seconds,
            "element_max_seconds": self.element_max_seconds,
            "retry_truncated_element": self.retry_truncated_element,
            "coordinate_scale": self.coordinate_scale,
            "crop_padding_ratio": self.crop_padding_ratio,
            "prompt_version": self.prompt_version,
            "adapter_version": self.adapter_version,
            "pipeline_version": self.pipeline_version,
            "parsing_mode": self.parsing_mode,
            "pdf_render_dpi": self.pdf_render_dpi,
        }
        return {
            "model_id": self.model_id,
            "revision": self.revision,
            "pipeline_version": self.pipeline_version,
            "dpi": self.pdf_render_dpi,
            "monkeyocr": monkeyocr,
        }

    def settings_hash(self) -> str:
        payload = json.dumps(
            self.effective_inference_settings(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def engine_config(self, project_root: Path) -> dict[str, Any]:
        values = self.model_dump(mode="python")
        for key in ("cache_dir", "modules_cache_dir", "element_cache_dir", "model_call_journal"):
            value = values.get(key)
            if isinstance(value, Path) and not value.is_absolute():
                values[key] = (project_root / value).resolve()
        return values
