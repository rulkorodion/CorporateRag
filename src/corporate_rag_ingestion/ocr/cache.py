from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import OCRFingerprint, OCRPageIdentity, OCRPageResult


class OCRCache:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @staticmethod
    def _safe_component(value: str, label: str) -> str:
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError(f"unsafe OCR cache {label}: {value!r}")
        return value

    def result_path(self, identity: OCRPageIdentity) -> Path:
        document_id = self._safe_component(identity.document_id, "document_id")
        version_id = self._safe_component(identity.version_id, "version_id")
        return self.root / "pages" / document_id / version_id / f"page_{identity.page_number:04d}.json"

    def load(self, identity: OCRPageIdentity) -> OCRPageResult | None:
        path = self.result_path(identity)
        if not path.is_file():
            return None
        try:
            return OCRPageResult.model_validate_json(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None

    def reusable(self, identity: OCRPageIdentity, fingerprint: OCRFingerprint) -> bool:
        result = self.load(identity)
        return bool(result and result.reusable and result.fingerprint == fingerprint)

    def save(self, result: OCRPageResult) -> Path:
        target = self.result_path(result)
        target.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.stem}-", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(result.model_dump(mode="json"), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        return target

    def iter_results(self) -> list[OCRPageResult]:
        results: list[OCRPageResult] = []
        for path in sorted((self.root / "pages").rglob("page_*.json")):
            try:
                results.append(
                    OCRPageResult.model_validate_json(path.read_text(encoding="utf-8-sig"))
                )
            except (OSError, ValueError):
                continue
        return results
