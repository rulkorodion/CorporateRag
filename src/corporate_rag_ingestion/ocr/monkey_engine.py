"""Neutral two-stage MonkeyOCRv2 engine adapted from Specification Checker.

The full page is used only for layout detection. Every recognized element is
cropped from the original 300-DPI render and processed separately. Pictures are
kept as layout elements and never sent to text generation.
"""

from __future__ import annotations

import ast
import gc
import gzip
import hashlib
import html
import io
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from .base import OCREngine
from .models import (
    OCRBlock,
    OCRBoundingBox,
    OCREnginePage,
    OCRFormula,
    OCRLayoutElement,
    OCRPageGeometry,
    OCRTable,
    OCRTextPayload,
)
from .reading_order import normalized_block_text
from .settings import MonkeyOCRSettings


OFFICIAL_PROMPTS = {
    "Caption": "Please output the text content from the image.",
    "List-item": "Please output the text content from the image.",
    "Page-footer": "Please output the text content from the image.",
    "Page-header": "Please output the text content from the image.",
    "Section-header": "Please output the text content from the image.",
    "Text": "Please output the text content from the image.",
    "Title": "Please output the text content from the image.",
    "Formula": "Please write out the expression of the formula in the image using LaTeX format.",
    "Table": "Please extract the table from the image and represent it in OTSL format.",
    "LAYOUT": "Please output the categories and coordinates of the document elements in reading order.",
}
TEXT_LABELS = {
    "Caption", "List-item", "Page-footer", "Page-header", "Section-header",
    "Text", "Title", "Formula", "Table",
}
MAIN_TEXT_LABELS = {"Caption", "List-item", "Section-header", "Text", "Title"}


def _balanced_fragments(text: str, left: str, right: str) -> list[str]:
    result: list[str] = []
    depth = 0
    start = -1
    for index, character in enumerate(text):
        if character == left:
            if depth == 0:
                start = index
            depth += 1
        elif character == right and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                result.append(text[start : index + 1])
                start = -1
    return result


def has_complete_layout_payload(raw: str) -> bool:
    for fragment in _balanced_fragments(raw, "[", "]"):
        try:
            value = json.loads(fragment)
        except json.JSONDecodeError:
            try:
                value = ast.literal_eval(fragment)
            except (SyntaxError, ValueError):
                continue
        if isinstance(value, list) and any(
            isinstance(item, dict)
            and isinstance(item.get("bbox"), (list, tuple))
            and len(item["bbox"]) == 4
            and item.get("label")
            for item in value
        ):
            return True
    return False


def parse_layout_output(raw: str) -> tuple[list[dict[str, Any]], bool]:
    text = re.sub(r"^```(?:json|python)?\s*|\s*```$", "", raw.strip(), flags=re.I)
    parsed: list[Any] = []
    repaired = False
    for candidate in (text, *_balanced_fragments(text, "[", "]")):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            try:
                value = ast.literal_eval(candidate)
            except (SyntaxError, ValueError):
                continue
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            parsed = value
            repaired = candidate != text
            break
    if not parsed:
        repaired = True
        for fragment in _balanced_fragments(text, "{", "}"):
            try:
                value = json.loads(fragment)
            except json.JSONDecodeError:
                try:
                    value = ast.literal_eval(fragment)
                except (SyntaxError, ValueError):
                    continue
            if isinstance(value, dict):
                parsed.append(value)
    result: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        bbox = item.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        try:
            numeric = [float(value) for value in bbox]
        except (TypeError, ValueError):
            continue
        result.append({"bbox": numeric, "label": str(item.get("label") or "Text")})
    return result, repaired


def layout_bbox_to_original(
    bbox: list[float] | tuple[float, float, float, float],
    *,
    bbox_source_width: int,
    bbox_source_height: int,
    original_page_width: int,
    original_page_height: int,
    coordinate_scale: float = 1000.0,
    padding_ratio: float = 0.0,
) -> tuple[int, int, int, int]:
    if coordinate_scale <= 0:
        raise ValueError("coordinate_scale must be positive")
    if min(bbox_source_width, bbox_source_height, original_page_width, original_page_height) <= 0:
        raise ValueError("page dimensions must be positive")
    x1, y1, x2, y2 = (float(value) for value in bbox)
    source = [
        x1 / coordinate_scale * bbox_source_width,
        y1 / coordinate_scale * bbox_source_height,
        x2 / coordinate_scale * bbox_source_width,
        y2 / coordinate_scale * bbox_source_height,
    ]
    if source[0] > source[2]:
        source[0], source[2] = source[2], source[0]
    if source[1] > source[3]:
        source[1], source[3] = source[3], source[1]
    scale_x = original_page_width / bbox_source_width
    scale_y = original_page_height / bbox_source_height
    mapped = [source[0] * scale_x, source[1] * scale_y, source[2] * scale_x, source[3] * scale_y]
    pad_x = max(0.0, padding_ratio) * original_page_width
    pad_y = max(0.0, padding_ratio) * original_page_height
    mapped = [mapped[0] - pad_x, mapped[1] - pad_y, mapped[2] + pad_x, mapped[3] + pad_y]
    out_x1 = max(0, min(original_page_width - 1, int(round(mapped[0]))))
    out_y1 = max(0, min(original_page_height - 1, int(round(mapped[1]))))
    out_x2 = max(out_x1 + 1, min(original_page_width, int(round(mapped[2]))))
    out_y2 = max(out_y1 + 1, min(original_page_height, int(round(mapped[3]))))
    return out_x1, out_y1, out_x2, out_y2


def _clean_text(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"[ \t]+", " ", value).strip()


def _otsl_rows(content: str) -> list[list[str]]:
    if "<fcel>" not in content and "<ecel>" not in content:
        return []
    rows: list[list[str]] = []
    for raw_row in content.split("<nl>"):
        cells = [
            _clean_text(match.group(1))
            for match in re.finditer(
                r"<(?:fcel|ecel)>(.*?)(?=<(?:fcel|ecel|lcel|ucel|xcel)>|$)",
                raw_row,
                flags=re.S,
            )
        ]
        if cells:
            rows.append(cells)
    return rows


def otsl_to_html(content: str) -> str:
    rows = _otsl_rows(content)
    if not rows:
        return content.strip()
    return "<table>" + "".join(
        "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    ) + "</table>"


def _native_content(label: str, raw: str) -> str:
    content = raw.strip()
    if label == "Table":
        return otsl_to_html(content)
    if label == "Formula":
        return f"$$\n{content.strip('$').strip()}\n$$" if content else ""
    if label == "Title":
        return "# " + content.replace("\n", "\n# ") if content else ""
    if label == "Section-header":
        return "## " + content.replace("\n", "\n## ") if content else ""
    return content


def _line_pieces(content: str, bbox: tuple[int, int, int, int]) -> list[tuple[str, tuple[float, float, float, float]]]:
    lines = [_clean_text(line) for line in content.splitlines() if _clean_text(line)]
    if not lines and _clean_text(content):
        lines = [_clean_text(content)]
    x1, y1, x2, y2 = bbox
    height = (y2 - y1) / max(1, len(lines))
    return [(line, (x1, y1 + index * height, x2, y1 + (index + 1) * height)) for index, line in enumerate(lines)]


def _table_pieces(rows: list[list[str]], bbox: tuple[int, int, int, int]) -> list[tuple[str, tuple[float, float, float, float]]]:
    x1, y1, x2, y2 = bbox
    row_height = (y2 - y1) / max(1, len(rows))
    pieces: list[tuple[str, tuple[float, float, float, float]]] = []
    for row_index, row in enumerate(rows):
        width = (x2 - x1) / max(1, len(row))
        for column_index, text in enumerate(row):
            if text:
                pieces.append((text, (x1 + column_index * width, y1 + row_index * row_height, x1 + (column_index + 1) * width, y1 + (row_index + 1) * row_height)))
    return pieces


def _repeat_tail_detected(value: str) -> bool:
    tail = (value or "")[-500:]
    return any(
        (fragment := tail[-size:]) and tail.endswith(fragment * 6)
        for size in range(1, min(100, len(tail) // 2) + 1)
    )


@dataclass(slots=True)
class InferenceOutput:
    text: str
    finish_reason: str
    truncated: bool
    generated_tokens: int
    input_size: tuple[int, int]


class CropRecognitionCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json.gz"

    def get(self, key: str) -> InferenceOutput | None:
        try:
            with gzip.open(self._path(key), "rt", encoding="utf-8") as stream:
                return InferenceOutput(**json.load(stream))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return None

    def put(self, key: str, output: InferenceOutput) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with gzip.open(temporary, "wt", encoding="utf-8") as stream:
            json.dump(
                {
                    "text": output.text,
                    "finish_reason": output.finish_reason,
                    "truncated": output.truncated,
                    "generated_tokens": output.generated_tokens,
                    "input_size": output.input_size,
                },
                stream,
                ensure_ascii=False,
            )
        temporary.replace(path)


class MonkeyOCRParsingEngine(OCREngine):
    """Pinned two-stage parser. One instance loads the model once per worker."""

    def __init__(self, settings: MonkeyOCRSettings, project_root: Path) -> None:
        self.settings = settings
        self.config = settings.engine_config(project_root)
        self._model: Any = None
        self._processor: Any = None
        self._torch: Any = None
        self.actual_device = "uninitialized"
        self.effective_dtype = "uninitialized"
        self.element_cache = CropRecognitionCache(Path(self.config["element_cache_dir"]))

    @property
    def engine_name(self) -> str:
        return f"MonkeyOCRv2-B-Parsing (layout-crops-v1;transformers:{self.actual_device})"

    def prepare(self) -> dict[str, Any]:
        if self._model is not None:
            return self.runtime_info()
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
        if self.config.get("cache_dir"):
            os.environ["HF_HOME"] = str(self.config["cache_dir"])
        modules_cache = Path(self.config["modules_cache_dir"])
        modules_cache.mkdir(parents=True, exist_ok=True)
        os.environ["HF_MODULES_CACHE"] = str(modules_cache)
        try:
            import torch
            from huggingface_hub import snapshot_download
            from transformers import AutoConfig, AutoModelForCausalLM, AutoProcessor
        except ImportError as exc:
            raise RuntimeError(
                "MonkeyOCR dependencies are missing; install requirements-monkeyocr.txt on the GPU worker"
            ) from exc
        use_cuda = self.settings.device != "cpu" and torch.cuda.is_available()
        if self.settings.device.startswith("gpu") and not use_cuda:
            raise RuntimeError("MonkeyOCR GPU requested but CUDA is unavailable")
        self.actual_device = "gpu:0" if use_cuda else "cpu"
        if not use_cuda:
            dtype = torch.float32
            self.effective_dtype = "float32"
        elif self.settings.dtype == "float16":
            dtype = torch.float16
            self.effective_dtype = "float16"
        elif torch.cuda.is_bf16_supported():
            dtype = torch.bfloat16
            self.effective_dtype = "bfloat16"
        else:
            dtype = torch.float16
            self.effective_dtype = "float16"
        model_source = snapshot_download(
            repo_id=self.settings.model_id,
            revision=self.settings.revision,
            local_files_only=True,
        )
        self._processor = AutoProcessor.from_pretrained(
            model_source,
            trust_remote_code=True,
            local_files_only=True,
            use_fast=True,
        )
        model_config = AutoConfig.from_pretrained(
            model_source,
            trust_remote_code=True,
            local_files_only=True,
        )
        setattr(model_config, "_attn_implementation", "sdpa")
        vision_config = getattr(model_config, "vision_config", None)
        if vision_config is not None:
            setattr(vision_config, "attn_implementation", "sdpa")
        self._model = AutoModelForCausalLM.from_pretrained(
            model_source,
            trust_remote_code=True,
            local_files_only=True,
            device_map="auto" if use_cuda else None,
            dtype=dtype,
            attn_implementation="sdpa",
            config=model_config,
        )
        if not use_cuda:
            self._model.to("cpu")
        self._model.eval()
        self._torch = torch
        return self.runtime_info()

    def runtime_info(self) -> dict[str, Any]:
        return {
            "model_id": self.settings.model_id,
            "model_revision": self.settings.revision,
            "pipeline_version": "layout-crops-v1",
            "inference_profile": self.settings.profile,
            "requested_dtype": self.settings.dtype,
            "effective_dtype": self.effective_dtype,
            "attention_backend": "sdpa",
            "transformers_use_kv_cache": True,
            "do_sample": False,
            "actual_device": self.actual_device,
            "settings_hash": self.settings.settings_hash(),
        }

    def release(self) -> None:
        """Release model VRAM between OCR and embedding phases."""

        self._model = None
        self._processor = None
        gc.collect()
        if self._torch is not None and self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()
        self._torch = None
        self.actual_device = "released"
        self.effective_dtype = "released"

    @staticmethod
    def _resize(image: Image.Image, max_pixels: int) -> Image.Image:
        pixels = image.width * image.height
        if pixels <= max_pixels:
            return image
        scale = (max_pixels / pixels) ** 0.5
        return image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )

    def _infer(self, image: Image.Image, prompt: str, *, stage: str, max_tokens: int) -> InferenceOutput:
        self.prepare()
        prepared = self._resize(
            image,
            self.settings.layout_max_pixels if stage == "layout" else self.settings.recognition_max_pixels,
        )
        messages = [{"role": "user", "content": [{"type": "image", "image": prepared}, {"type": "text", "text": prompt}]}]
        inputs = self._processor.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
        )
        device = next(self._model.parameters()).device
        inputs = {key: value.to(device) if hasattr(value, "to") else value for key, value in inputs.items()}
        input_length = int(inputs["input_ids"].shape[1])
        from transformers import StoppingCriteria, StoppingCriteriaList

        processor = self._processor

        class RepeatedTailCriteria(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs) -> bool:  # type: ignore[no-untyped-def]
                generated = input_ids[0, input_length:]
                if int(generated.shape[0]) < 96 or int(generated.shape[0]) % 16:
                    return False
                return _repeat_tail_detected(processor.decode(generated, skip_special_tokens=True))

        criteria: list[StoppingCriteria] = [RepeatedTailCriteria()]
        if stage == "layout":
            class CompleteLayoutCriteria(StoppingCriteria):
                def __call__(self, input_ids, scores, **kwargs) -> bool:  # type: ignore[no-untyped-def]
                    generated = input_ids[0, input_length:]
                    return has_complete_layout_payload(processor.decode(generated, skip_special_tokens=True))
            criteria.append(CompleteLayoutCriteria())
        options: dict[str, Any] = {
            "max_new_tokens": max_tokens,
            "do_sample": False,
            "use_cache": True,
            "stopping_criteria": StoppingCriteriaList(criteria),
            "max_time": self.settings.layout_max_seconds if stage == "layout" else self.settings.element_max_seconds,
        }
        started = time.perf_counter()
        with self._torch.inference_mode():
            output_ids = self._model.generate(**inputs, **options)
        elapsed = time.perf_counter() - started
        generated = output_ids[:, input_length:]
        generated_tokens = int(generated.shape[1])
        text = self._processor.batch_decode(generated, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]
        repeated = _repeat_tail_detected(text)
        complete = stage != "layout" or has_complete_layout_payload(text)
        time_limit = self.settings.layout_max_seconds if stage == "layout" else self.settings.element_max_seconds
        timed_out = elapsed >= time_limit * 0.98
        truncated = generated_tokens >= max_tokens or repeated or timed_out or not complete
        reason = "repetition" if repeated else "time_limit" if timed_out else "incomplete" if not complete else "length" if truncated else "stop"
        del generated, output_ids, inputs
        gc.collect()
        if self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()
        return InferenceOutput(text, reason, truncated, generated_tokens, prepared.size)

    @staticmethod
    def _crop_hash(crop: Image.Image) -> str:
        buffer = io.BytesIO()
        crop.convert("RGB").save(buffer, format="PNG", optimize=True)
        return hashlib.sha256(buffer.getvalue()).hexdigest()

    def _recognize_crop(self, crop: Image.Image, label: str) -> tuple[InferenceOutput, str, bool]:
        crop_hash = self._crop_hash(crop)
        key_payload = {
            "crop_sha256": crop_hash,
            "label": label,
            "prompt": OFFICIAL_PROMPTS[label],
            **self.settings.effective_inference_settings(),
        }
        key = hashlib.sha256(json.dumps(key_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        cached = self.element_cache.get(key)
        if cached is not None:
            return cached, crop_hash, True
        output = self._infer(crop, OFFICIAL_PROMPTS[label], stage="recognition", max_tokens=self.settings.element_max_tokens)
        if output.truncated and output.finish_reason == "length" and label in {"Table", "Formula"}:
            output = self._infer(crop, OFFICIAL_PROMPTS[label], stage="recognition", max_tokens=min(self.settings.element_max_tokens * 2, 10000))
        if not output.truncated:
            self.element_cache.put(key, output)
        return output, crop_hash, False

    def recognize_page(
        self,
        image: Image.Image,
        *,
        page_number: int,
        preprocessing_profile: str,
    ) -> OCREnginePage:
        original = image.convert("RGB")
        width, height = original.size
        layout_output = self._infer(original, OFFICIAL_PROMPTS["LAYOUT"], stage="layout", max_tokens=self.settings.layout_max_tokens)
        if layout_output.truncated:
            raise RuntimeError(f"MonkeyOCR incomplete layout on page {page_number}: {layout_output.finish_reason}")
        items, repaired = parse_layout_output(layout_output.text)
        if not items:
            raise RuntimeError(f"MonkeyOCR returned no layout elements on page {page_number}")
        blocks: list[OCRBlock] = []
        elements: list[OCRLayoutElement] = []
        tables: list[OCRTable] = []
        formulas: list[OCRFormula] = []
        headers: list[OCRLayoutElement] = []
        footers: list[OCRLayoutElement] = []
        markdown: list[str] = []
        main_text: list[str] = []
        truncated_elements: list[int] = []
        layout_width, layout_height = layout_output.input_size
        for index, item in enumerate(items):
            label = item["label"]
            bbox = layout_bbox_to_original(
                item["bbox"],
                bbox_source_width=layout_width,
                bbox_source_height=layout_height,
                original_page_width=width,
                original_page_height=height,
                coordinate_scale=self.settings.coordinate_scale,
                padding_ratio=self.settings.crop_padding_ratio,
            )
            crop = original.crop(bbox)
            raw = ""
            raw_diagnostic = ""
            crop_hash = self._crop_hash(crop)
            metadata: dict[str, Any] = {"crop_cache_hit": False, "truncated": False}
            if label in TEXT_LABELS:
                output, crop_hash, cache_hit = self._recognize_crop(crop, label)
                raw_diagnostic = output.text.strip()
                raw = "" if output.truncated else raw_diagnostic
                metadata = {
                    "crop_cache_hit": cache_hit,
                    "truncated": output.truncated,
                    "finish_reason": output.finish_reason,
                    "model_confidence": None,
                }
                if output.truncated:
                    truncated_elements.append(index)
            native = _native_content(label, raw)
            element = OCRLayoutElement(
                block_type=label,
                page_number=page_number,
                reading_order=index,
                bbox_source_width=layout_width,
                bbox_source_height=layout_height,
                original_page_width=width,
                original_page_height=height,
                normalized_bbox=item["bbox"],
                original_pixel_bbox=list(bbox),
                text=_clean_text(native if label == "Table" else raw),
                raw_response=raw_diagnostic,
                native_content=native,
                crop_sha256=crop_hash,
                model_confidence=None,
                metadata=metadata,
            )
            elements.append(element)
            if label == "Page-header":
                headers.append(element)
            elif label == "Page-footer":
                footers.append(element)
            elif label != "Picture" and native:
                markdown.append(native)
            if label in MAIN_TEXT_LABELS and raw:
                main_text.append(_clean_text(raw))
            if label == "Table":
                tables.append(OCRTable(page_number=page_number, reading_order=index, bbox=list(bbox), otsl=raw, html=native, raw_response=raw_diagnostic))
            elif label == "Formula" and raw:
                formulas.append(OCRFormula(page_number=page_number, reading_order=index, bbox=list(bbox), latex=raw.strip("$ "), native_content=native))
            if label == "Picture":
                continue
            rows = _otsl_rows(raw) if label == "Table" else []
            pieces = _table_pieces(rows, bbox) if rows else _line_pieces(raw, bbox)
            for sub_index, (text, piece) in enumerate(pieces):
                blocks.append(
                    OCRBlock(
                        text=text,
                        confidence=None,
                        model_confidence=None,
                        bounding_box=OCRBoundingBox(
                            x_min=piece[0], y_min=piece[1], x_max=piece[2], y_max=piece[3],
                            page_width=width, page_height=height,
                        ),
                        page_number=page_number,
                        block_type=label,
                        reading_order=index * 1000 + sub_index,
                        source="monkeyocr-layout-crop",
                        metadata={"layout_reading_order": index, "model_confidence": None, **metadata},
                    )
                )
        normalized = normalized_block_text(blocks, height)
        native_markdown = "\n\n".join(markdown).strip()
        warnings = ["MonkeyOCR does not publish confidence; confidence=null and model_confidence=null"]
        if repaired:
            warnings.append("MonkeyOCR layout response was partially repaired")
        if truncated_elements:
            warnings.append("Truncated element output was excluded from OCR text")
        return OCREnginePage(
            page_number=page_number,
            page=OCRPageGeometry(width=width, height=height, dpi=self.settings.pdf_render_dpi),
            text=OCRTextPayload(
                text_for_rag=native_markdown or normalized,
                normalized_block_text=normalized,
                main_text="\n".join(main_text),
                native_markdown=native_markdown,
            ),
            blocks=blocks,
            layout_elements=elements,
            tables=tables,
            formulas=formulas,
            page_headers=headers,
            page_footers=footers,
            warnings=warnings,
            engine_name=self.engine_name,
            runtime=self.runtime_info(),
            processing_metadata={
                "layout_raw": layout_output.text,
                "layout_repaired": repaired,
                "possible_output_truncation": bool(truncated_elements),
                "truncated_elements": truncated_elements,
            },
        )


MonkeyOCRv2Engine = MonkeyOCRParsingEngine
