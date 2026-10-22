from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..schemas import NormalizedDocument
from ..scanner import sha256_file
from .cache import OCRCache
from .models import OCRFingerprint, OCRManifestItem, OCRPageIdentity
from .settings import MonkeyOCRSettings


@dataclass(slots=True)
class ManifestSummary:
    documents_considered: int = 0
    candidate_pages: int = 0
    reusable_completed: int = 0
    selected_pages: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "documents_considered": self.documents_considered,
            "candidate_pages": self.candidate_pages,
            "reusable_completed": self.reusable_completed,
            "selected_pages": self.selected_pages,
        }


class OCRManifestGenerator:
    def __init__(
        self,
        *,
        normalized_root: Path,
        knowledge_base_root: Path,
        cache: OCRCache,
        settings: MonkeyOCRSettings,
    ) -> None:
        self.normalized_root = normalized_root
        self.knowledge_base_root = knowledge_base_root.resolve()
        self.cache = cache
        self.settings = settings

    def generate(self) -> tuple[list[OCRManifestItem], ManifestSummary]:
        tasks: list[OCRManifestItem] = []
        summary = ManifestSummary()
        source_hashes: dict[Path, str] = {}
        for path in sorted(self.normalized_root.rglob("*.json")):
            document = NormalizedDocument.model_validate_json(path.read_text(encoding="utf-8"))
            if document.metadata.get("extension") != ".pdf":
                continue
            relative_path = str(document.metadata.get("relative_path") or "").replace("\\", "/")
            if not relative_path:
                continue
            source_path = (self.knowledge_base_root / relative_path).resolve()
            if not source_path.is_relative_to(self.knowledge_base_root) or not source_path.is_file():
                raise FileNotFoundError(f"manifest PDF source not found: {relative_path}")
            expected_sha = str(document.metadata.get("sha256") or "").casefold()
            candidate_pages = [
                page
                for page in document.pages
                if page.page_visual_type == "scan_candidate" and page.needs_ocr
            ]
            if not candidate_pages:
                continue
            summary.documents_considered += 1
            actual_sha = source_hashes.setdefault(source_path, sha256_file(source_path))
            if actual_sha != expected_sha:
                raise ValueError(f"manifest source SHA-256 mismatch: {relative_path}")
            for page in candidate_pages:
                summary.candidate_pages += 1
                identity = OCRPageIdentity(
                    document_id=document.document_id,
                    version_id=document.version_id,
                    relative_path=relative_path,
                    page_number=page.page_number,
                    source_sha256=expected_sha,
                )
                fingerprint = OCRFingerprint(
                    **identity.model_dump(),
                    ocr_model_id=self.settings.model_id,
                    ocr_model_revision=self.settings.revision,
                    ocr_settings_hash=self.settings.settings_hash(),
                )
                if self.cache.reusable(identity, fingerprint):
                    summary.reusable_completed += 1
                    continue
                tasks.append(
                    OCRManifestItem(
                        **identity.model_dump(),
                        input_file=relative_path,
                        fingerprint=fingerprint,
                    )
                )
        summary.selected_pages = len(tasks)
        return tasks, summary

    @staticmethod
    def save(path: Path, tasks: list[OCRManifestItem], summary: ManifestSummary) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(
            json.dumps(task.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
            + "\n"
            for task in tasks
        )
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        summary_path = path.with_suffix(".summary.json")
        summary_path.write_text(
            json.dumps(summary.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def load_manifest(path: Path) -> list[OCRManifestItem]:
    tasks: list[OCRManifestItem] = []
    seen: set[tuple[str, str, int]] = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not raw.strip():
            continue
        task = OCRManifestItem.model_validate_json(raw)
        key = (task.document_id, task.version_id, task.page_number)
        if key in seen:
            raise ValueError(f"duplicate OCR manifest task at line {line_number}: {key}")
        seen.add(key)
        tasks.append(task)
    return tasks
