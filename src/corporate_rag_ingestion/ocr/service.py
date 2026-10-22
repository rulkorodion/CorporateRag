from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pymupdf as fitz
from PIL import Image

from ..scanner import sha256_file
from .base import OCREngine
from .cache import OCRCache
from .models import (
    OCRFingerprint,
    OCRManifestItem,
    OCRPageIdentity,
    OCRPageResult,
    OCRProvenance,
)
from .settings import MonkeyOCRSettings


def render_pdf_page(path: Path, page_number: int, dpi: int) -> Image.Image:
    with fitz.open(path) as pdf:
        if pdf.needs_pass:
            raise ValueError(f"password-protected PDF: {path}")
        if page_number < 1 or page_number > pdf.page_count:
            raise ValueError(f"page {page_number} outside PDF page count {pdf.page_count}: {path}")
        scale = dpi / 72.0
        pixmap = pdf[page_number - 1].get_pixmap(
            matrix=fitz.Matrix(scale, scale),
            alpha=False,
            colorspace=fitz.csRGB,
        )
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


class OCRService:
    def __init__(
        self,
        *,
        cache: OCRCache,
        settings: MonkeyOCRSettings,
        project_root: Path,
        engine: OCREngine | None = None,
        engine_factory: Callable[[], OCREngine] | None = None,
    ) -> None:
        self.cache = cache
        self.settings = settings
        self.project_root = project_root
        self._engine = engine
        self._engine_factory = engine_factory
        self._engine_prepared = False

    def fingerprint(self, identity: OCRPageIdentity) -> OCRFingerprint:
        return OCRFingerprint(
            **identity.model_dump(),
            ocr_model_id=self.settings.model_id,
            ocr_model_revision=self.settings.revision,
            ocr_settings_hash=self.settings.settings_hash(),
        )

    def _get_engine(self) -> OCREngine:
        if self._engine is None:
            if self._engine_factory is not None:
                self._engine = self._engine_factory()
            else:
                from .monkey_engine import MonkeyOCRParsingEngine

                self._engine = MonkeyOCRParsingEngine(self.settings, self.project_root)
        if not self._engine_prepared:
            self._engine.prepare()
            self._engine_prepared = True
        return self._engine

    def process_page(
        self,
        source_path: Path,
        identity: OCRPageIdentity,
        *,
        allow_monkeyocr: bool = False,
    ) -> tuple[OCRPageResult, bool]:
        fingerprint = self.fingerprint(identity)
        cached = self.cache.load(identity)
        if cached and cached.reusable and cached.fingerprint == fingerprint:
            return cached, True
        if not allow_monkeyocr:
            raise RuntimeError(
                "MonkeyOCR execution is disabled; import a completed OCR JSON or explicitly enable future GPU processing"
            )
        actual_sha = sha256_file(source_path)
        if actual_sha != identity.source_sha256:
            raise ValueError(f"OCR source SHA-256 mismatch: {identity.relative_path}")
        started = time.perf_counter()
        image = render_pdf_page(source_path, identity.page_number, self.settings.pdf_render_dpi)
        engine = self._get_engine()
        page = engine.recognize_page(
            image,
            page_number=identity.page_number,
            preprocessing_profile="original",
        )
        partial = bool(page.processing_metadata.get("possible_output_truncation"))
        if not page.text.text_for_rag.strip():
            partial = True
            page.warnings.append("MonkeyOCR returned no textual content for the page")
        status = "PARTIAL" if partial else "COMPLETED"
        result = OCRPageResult(
            **identity.model_dump(),
            status=status,
            page=page.page,
            text=page.text,
            blocks=page.blocks,
            layout_elements=page.layout_elements,
            tables=page.tables,
            formulas=page.formulas,
            page_headers=page.page_headers,
            page_footers=page.page_footers,
            warnings=page.warnings,
            provenance=OCRProvenance(
                ocr_source="monkeyocr",
                engine_name=page.engine_name,
                ocr_model_id=self.settings.model_id,
                ocr_model_revision=self.settings.revision,
                ocr_settings_hash=self.settings.settings_hash(),
                processed_at=datetime.now(timezone.utc),
                runtime=page.runtime,
                processing_metadata=page.processing_metadata,
            ),
            fingerprint=fingerprint,
            processing_time_seconds=time.perf_counter() - started,
        )
        self.cache.save(result)
        return result, False

    def release(self) -> None:
        if self._engine is not None:
            release = getattr(self._engine, "release", None)
            if callable(release):
                release()
        self._engine = None
        self._engine_prepared = False

    def process_manifest(
        self,
        tasks: list[OCRManifestItem],
        *,
        knowledge_base_root: Path,
        shard_index: int,
        shard_count: int,
        allow_monkeyocr: bool,
        on_completed: Callable[[OCRPageResult, Path], bool] | None = None,
    ) -> dict[str, Any]:
        if shard_count < 1 or not 0 <= shard_index < shard_count:
            raise ValueError("shard_index must satisfy 0 <= shard_index < shard_count")
        selected = [task for index, task in enumerate(tasks) if index % shard_count == shard_index]
        counts = {"COMPLETED": 0, "PARTIAL": 0, "FAILED": 0, "SKIPPED": 0}
        integration = {"integrated": 0, "skipped": 0, "failed": 0}
        errors: list[str] = []
        for task in selected:
            identity = OCRPageIdentity(
                document_id=task.document_id,
                version_id=task.version_id,
                relative_path=task.relative_path,
                page_number=task.page_number,
                source_sha256=task.source_sha256,
            )
            expected_fingerprint = self.fingerprint(identity)
            if task.fingerprint != expected_fingerprint:
                raise ValueError(f"manifest fingerprint mismatch: {task.relative_path}#page={task.page_number}")
            source_path = (knowledge_base_root / task.input_file).resolve()
            if not source_path.is_relative_to(knowledge_base_root.resolve()):
                raise ValueError(f"manifest source escapes knowledge-base root: {task.input_file}")
            try:
                result, reused = self.process_page(
                    source_path,
                    identity,
                    allow_monkeyocr=allow_monkeyocr,
                )
                counts["SKIPPED" if reused else result.status] += 1
                if result.status == "COMPLETED" and on_completed is not None:
                    try:
                        changed = on_completed(result, source_path)
                        integration["integrated" if changed else "skipped"] += 1
                    except Exception as exc:
                        integration["failed"] += 1
                        errors.append(
                            f"integration {task.relative_path}#page={task.page_number}: {exc}"
                        )
            except Exception as exc:
                failed = OCRPageResult(
                    **identity.model_dump(),
                    status="FAILED",
                    fingerprint=expected_fingerprint,
                    error={
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                )
                self.cache.save(failed)
                counts["FAILED"] += 1
                errors.append(f"{task.relative_path}#page={task.page_number}: {exc}")
        return {
            "shard_index": shard_index,
            "shard_count": shard_count,
            "selected_tasks": len(selected),
            "counts": counts,
            "integration": integration,
            "errors": errors,
            "ocr_settings_hash": self.settings.settings_hash(),
        }
