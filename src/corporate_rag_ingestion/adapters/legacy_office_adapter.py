from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from .base import AdapterContext, FormatAdapter
from .docling_adapter import DoclingAdapter


class LegacyOfficePrerequisiteError(RuntimeError):
    pass


def find_soffice(explicit_path: Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(explicit_path)
    for command in ("soffice", "libreoffice"):
        resolved = shutil.which(command)
        if resolved:
            candidates.append(Path(resolved))
    candidates.extend(
        [
            Path("C:/Program Files/LibreOffice/program/soffice.exe"),
            Path("C:/Program Files (x86)/LibreOffice/program/soffice.exe"),
            Path("/usr/bin/libreoffice"),
            Path("/usr/bin/soffice"),
        ]
    )
    return next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)


class LegacyOfficeAdapter(FormatAdapter):
    """Convert legacy Office files in cache, then reuse the stable Docling adapter."""

    def __init__(
        self,
        docling_adapter: DoclingAdapter,
        cache_root: Path,
        *,
        libreoffice_path: Path | None = None,
        conversion_timeout_seconds: float = 180.0,
    ) -> None:
        self.docling_adapter = docling_adapter
        self.cache_root = cache_root
        self.libreoffice_path = libreoffice_path
        self.conversion_timeout_seconds = conversion_timeout_seconds

    def normalize(self, context: AdapterContext):  # type: ignore[no-untyped-def]
        extension = context.source_path.suffix.casefold()
        if extension not in {".doc", ".rtf"}:
            raise ValueError(f"LegacyOfficeAdapter does not support {extension}")
        soffice = find_soffice(self.libreoffice_path)
        if soffice is None:
            raise LegacyOfficePrerequisiteError(
                "LibreOffice (soffice) is required to normalize legacy .doc/.rtf files. "
                "Install LibreOffice and ensure soffice is on PATH, or set LIBREOFFICE_PATH."
            )
        converted = self._convert_to_docx(context, soffice)
        converted_context = replace(
            context,
            source_path=converted,
            metadata={
                **context.metadata,
                "legacy_conversion": {
                    "source_format": extension,
                    "converter": "libreoffice_headless",
                    "cached_docx": context.storage_relative(converted),
                },
            },
        )
        return self.docling_adapter.normalize(converted_context)

    def _convert_to_docx(self, context: AdapterContext, soffice: Path) -> Path:
        # LibreOffice on Windows can fail silently (exit 0, no output file) once the
        # working path gets too deep. Keep the cache key stable but deliberately short.
        cache_key = hashlib.sha256(
            f"{context.document_id}:{context.version_id}".encode("utf-8")
        ).hexdigest()[:24]
        cache_directory = self.cache_root / cache_key
        cache_directory.mkdir(parents=True, exist_ok=True)
        cached_source = cache_directory / f"source{context.source_path.suffix.casefold()}"
        converted = cache_directory / "source.docx"
        user_profile = cache_directory / "libreoffice-profile"
        if converted.is_file() and converted.stat().st_mtime_ns >= context.source_path.stat().st_mtime_ns:
            return converted
        shutil.copy2(context.source_path, cached_source)
        command = [
            str(soffice),
            f"-env:UserInstallation={user_profile.resolve().as_uri()}",
            "--headless",
            "--convert-to",
            "docx",
            "--outdir",
            str(cache_directory),
            str(cached_source),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.conversion_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"LibreOffice conversion timed out after {self.conversion_timeout_seconds:g}s: "
                f"{context.source_path}"
            ) from exc
        if completed.returncode != 0 or not converted.is_file():
            diagnostic = (completed.stderr or completed.stdout or "no diagnostic output").strip()
            raise RuntimeError(
                f"LibreOffice failed to convert {context.source_path.name} to DOCX "
                f"(exit {completed.returncode}): {diagnostic}"
            )
        return converted
