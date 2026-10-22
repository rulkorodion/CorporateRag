from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..scanner import sha256_file
from ..schemas import NormalizedDocument
from ..storage import NormalizedStorage
from .cache import OCRCache
from .imported import ImportedOCRParser, read_import_records
from .integration import OCRIntegrator
from .models import OCRPageIdentity, OCRPageResult
from .settings import MonkeyOCRSettings


@dataclass(slots=True)
class ImportSummary:
    records: int = 0
    validated: int = 0
    cached: int = 0
    integrated: int = 0
    skipped: int = 0
    partial: int = 0
    failed: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "records": self.records,
            "validated": self.validated,
            "cached": self.cached,
            "integrated": self.integrated,
            "skipped": self.skipped,
            "partial": self.partial,
            "failed": self.failed,
            "rejected": self.rejected,
            "errors": self.errors,
        }


class OCRImporter:
    def __init__(
        self,
        *,
        storage: NormalizedStorage,
        knowledge_base_root: Path,
        cache: OCRCache,
        settings: MonkeyOCRSettings,
        allow_ambiguous: bool = False,
    ) -> None:
        self.storage = storage
        self.knowledge_base_root = knowledge_base_root.resolve()
        self.cache = cache
        self.parser = ImportedOCRParser(settings)
        self.integrator = OCRIntegrator()
        self.allow_ambiguous = allow_ambiguous

    def _normalized_path(self, document_id: str, version_id: str) -> Path:
        # Reject path-shaped identifiers before they ever reach the filesystem.
        for value, label in ((document_id, "document_id"), (version_id, "version_id")):
            if not value or value in {".", ".."} or "/" in value or "\\" in value:
                raise ValueError(f"unsafe imported OCR {label}: {value!r}")
        path = (self.storage.normalized_root / document_id / f"{version_id}.json").resolve()
        if not path.is_relative_to(self.storage.normalized_root.resolve()):
            raise ValueError("imported OCR document/version escapes normalized storage")
        return path

    def integrate_completed(
        self,
        result: OCRPageResult,
        source_path: Path | None = None,
    ) -> bool:
        """Persist a validated completed result from either OCR source.

        Returns ``True`` when the NormalizedDocument changed and ``False``
        when the exact fingerprint had already been integrated.
        """
        if result.status != "COMPLETED":
            raise ValueError("integrate_completed requires a COMPLETED OCRPageResult")
        normalized_path = self._normalized_path(result.document_id, result.version_id)
        if not normalized_path.is_file():
            raise FileNotFoundError(
                "NormalizedDocument not found for document/version: "
                f"{result.document_id}/{result.version_id}"
            )
        document = NormalizedDocument.model_validate_json(
            normalized_path.read_text(encoding="utf-8")
        )
        expected_source = (self.knowledge_base_root / result.relative_path).resolve()
        if not expected_source.is_relative_to(self.knowledge_base_root):
            raise ValueError(f"OCR source escapes knowledge-base root: {result.relative_path}")
        if source_path is not None and source_path.resolve() != expected_source:
            raise ValueError(f"OCR source path mismatch: {result.relative_path}")
        if not expected_source.is_file():
            raise FileNotFoundError(f"OCR source PDF not found: {result.relative_path}")
        if sha256_file(expected_source) != result.source_sha256:
            raise ValueError(f"current source SHA-256 mismatch: {result.relative_path}")
        self.cache.save(result)
        page = next(
            (item for item in document.pages if item.page_number == result.page_number),
            None,
        )
        if page is None:
            raise ValueError(f"normalized page not found: {result.page_number}")
        fingerprint_digest = result.fingerprint.digest()
        history = list(page.metadata.get("ocr_history") or [])
        if page.ocr_status == "completed" and any(
            item.get("fingerprint") == fingerprint_digest for item in history
        ):
            return False
        self.integrator.integrate(
            document,
            result,
            source_path=expected_source,
            storage_root=self.storage.storage_root,
            allow_ambiguous=self.allow_ambiguous,
        )
        self.storage.save(document)
        return True

    def import_path(self, path: Path, *, validate_only: bool = False) -> ImportSummary:
        records = read_import_records(path)
        summary = ImportSummary(records=len(records))
        seen: set[tuple[str, str, int]] = set()
        source_hashes: dict[Path, str] = {}
        for record in records:
            key = (
                str(record.get("document_id") or ""),
                str(record.get("version_id") or ""),
                int(record.get("page_number") or 0),
            )
            if key in seen:
                summary.rejected += 1
                summary.errors.append(f"duplicate imported OCR page: {key}")
                continue
            seen.add(key)
            try:
                normalized_path = self._normalized_path(key[0], key[1])
                if not normalized_path.is_file():
                    raise FileNotFoundError(
                        f"NormalizedDocument not found for document/version: {key[0]}/{key[1]}"
                    )
                document = NormalizedDocument.model_validate_json(
                    normalized_path.read_text(encoding="utf-8")
                )
                expected = OCRPageIdentity(
                    document_id=document.document_id,
                    version_id=document.version_id,
                    relative_path=str(document.metadata.get("relative_path") or ""),
                    page_number=key[2],
                    source_sha256=str(document.metadata.get("sha256") or ""),
                )
                result = self.parser.parse(record, expected)
                source_path = (self.knowledge_base_root / expected.relative_path).resolve()
                if not source_path.is_relative_to(self.knowledge_base_root) or not source_path.is_file():
                    raise FileNotFoundError(f"OCR source PDF not found: {expected.relative_path}")
                actual_sha = source_hashes.setdefault(source_path, sha256_file(source_path))
                if actual_sha != expected.source_sha256:
                    raise ValueError(f"current source SHA-256 mismatch: {expected.relative_path}")
                page = next(
                    (item for item in document.pages if item.page_number == result.page_number),
                    None,
                )
                if page is None:
                    raise ValueError(f"normalized page not found: {result.page_number}")
                allowed_types = {"scan_candidate"}
                if self.allow_ambiguous:
                    allowed_types.add("ambiguous")
                if page.page_visual_type not in allowed_types:
                    raise ValueError(
                        "OCR import is not approved for page classification "
                        f"{page.page_visual_type}"
                    )
                summary.validated += 1
                if validate_only:
                    continue
                self.cache.save(result)
                summary.cached += 1
                if result.status == "PARTIAL":
                    summary.partial += 1
                    continue
                if result.status == "FAILED":
                    summary.failed += 1
                    continue
                fingerprint_digest = result.fingerprint.digest()
                history = list(page.metadata.get("ocr_history") or [])
                if page.ocr_status == "completed" and any(
                    item.get("fingerprint") == fingerprint_digest for item in history
                ):
                    summary.skipped += 1
                    continue
                self.integrator.integrate(
                    document,
                    result,
                    source_path=source_path,
                    storage_root=self.storage.storage_root,
                    allow_ambiguous=self.allow_ambiguous,
                )
                self.storage.save(document)
                summary.integrated += 1
            except Exception as exc:
                summary.rejected += 1
                summary.errors.append(
                    f"{key[0]}/{key[1]}#page={key[2]}: {type(exc).__name__}: {exc}"
                )
        return summary
