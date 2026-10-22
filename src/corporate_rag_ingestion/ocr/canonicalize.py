from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..scanner import sha256_file
from ..schemas import NormalizedDocument
from .imported import ImportedOCRParser, read_import_records
from .models import OCRPageIdentity
from .settings import MonkeyOCRSettings


@dataclass(slots=True)
class CanonicalizeSummary:
    records: int = 0
    canonicalized: int = 0
    rejected: int = 0
    output_written: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "records": self.records,
            "canonicalized": self.canonicalized,
            "rejected": self.rejected,
            "output_written": self.output_written,
            "errors": self.errors,
        }


class OCRImportCanonicalizer:
    """Create an import-ready copy of OCR made with temporary batch IDs.

    Resolution requires the *exact* normalized relative path together with the
    full source SHA-256. A filename alone is never used. The strict importer is
    then run against the rewritten copy using all five canonical identity fields.
    """

    def __init__(
        self,
        *,
        normalized_root: Path,
        knowledge_base_root: Path,
        settings: MonkeyOCRSettings,
    ) -> None:
        self.normalized_root = normalized_root.resolve()
        self.knowledge_base_root = knowledge_base_root.resolve()
        self.parser = ImportedOCRParser(settings)

    def canonicalize(
        self,
        input_path: Path,
        output_path: Path,
        *,
        allow_ambiguous: bool = False,
    ) -> CanonicalizeSummary:
        records = read_import_records(input_path)
        summary = CanonicalizeSummary(records=len(records))
        index = self._build_index()
        converted: list[dict[str, Any]] = []
        seen: set[tuple[str, str, int]] = set()
        source_hashes: dict[Path, str] = {}

        for position, source_record in enumerate(records, start=1):
            relative_path = str(source_record.get("relative_path") or "").replace("\\", "/")
            source_sha = str(source_record.get("source_sha256") or "").casefold()
            page_number = int(source_record.get("page_number") or 0)
            label = f"record={position} {relative_path!r}#page={page_number}"
            try:
                matches = index.get((relative_path, source_sha), [])
                if len(matches) != 1:
                    raise ValueError(
                        "expected exactly one NormalizedDocument for relative_path + SHA-256, "
                        f"found {len(matches)}"
                    )
                document = matches[0]
                page = next(
                    (item for item in document.pages if item.page_number == page_number),
                    None,
                )
                if page is None:
                    raise ValueError("page is absent from NormalizedDocument")
                allowed_types = {"scan_candidate"}
                if allow_ambiguous:
                    allowed_types.add("ambiguous")
                if page.page_visual_type not in allowed_types:
                    raise ValueError(
                        "page classification is not approved for OCR import: "
                        f"{page.page_visual_type}"
                    )
                source_path = (self.knowledge_base_root / relative_path).resolve()
                if not source_path.is_relative_to(self.knowledge_base_root) or not source_path.is_file():
                    raise FileNotFoundError("source PDF is absent from the knowledge base")
                actual_sha = source_hashes.setdefault(source_path, sha256_file(source_path))
                if actual_sha != source_sha:
                    raise ValueError("current source PDF SHA-256 does not match the OCR export")

                record = copy.deepcopy(source_record)
                original_identity = {
                    "document_id": record.get("document_id"),
                    "version_id": record.get("version_id"),
                }
                record["document_id"] = document.document_id
                record["version_id"] = document.version_id
                fingerprint = record.get("fingerprint")
                if not isinstance(fingerprint, dict):
                    raise ValueError("OCR export has no fingerprint object")
                fingerprint["document_id"] = document.document_id
                fingerprint["version_id"] = document.version_id
                ocr = record.setdefault("ocr", {})
                if not isinstance(ocr, dict):
                    raise ValueError("OCR export has invalid ocr metadata")
                processing_metadata = ocr.setdefault("processing_metadata", {})
                if not isinstance(processing_metadata, dict):
                    raise ValueError("OCR export has invalid processing_metadata")
                processing_metadata["import_identity_rebound_from"] = original_identity
                if page.page_visual_type == "ambiguous":
                    processing_metadata["ocr_selection_override"] = {
                        "classification": "ambiguous",
                        "basis": "explicit_user_visual_review",
                    }

                expected = OCRPageIdentity(
                    document_id=document.document_id,
                    version_id=document.version_id,
                    relative_path=relative_path,
                    page_number=page_number,
                    source_sha256=source_sha,
                )
                self.parser.parse(record, expected)
                key = (document.document_id, document.version_id, page_number)
                if key in seen:
                    raise ValueError(f"duplicate canonical OCR page: {key}")
                seen.add(key)
                converted.append(record)
                summary.canonicalized += 1
            except Exception as exc:
                summary.rejected += 1
                summary.errors.append(f"{label}: {type(exc).__name__}: {exc}")

        if summary.rejected:
            return summary
        self._write_jsonl_atomic(output_path, converted)
        summary.output_written = True
        return summary

    def _build_index(self) -> dict[tuple[str, str], list[NormalizedDocument]]:
        index: dict[tuple[str, str], list[NormalizedDocument]] = {}
        for path in sorted(self.normalized_root.rglob("*.json")):
            document = NormalizedDocument.model_validate_json(path.read_text(encoding="utf-8"))
            if str(document.metadata.get("extension") or "").casefold() != ".pdf":
                continue
            relative_path = str(document.metadata.get("relative_path") or "").replace("\\", "/")
            source_sha = str(document.metadata.get("sha256") or "").casefold()
            index.setdefault((relative_path, source_sha), []).append(document)
        return index

    @staticmethod
    def _write_jsonl_atomic(path: Path, records: list[dict[str, Any]]) -> None:
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                for record in records:
                    stream.write(
                        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                    )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
