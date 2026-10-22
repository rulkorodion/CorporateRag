from __future__ import annotations

from pathlib import Path

from .adapters import (
    DoclingAdapter,
    ExcelAdapter,
    FormatAdapter,
    ImageAdapter,
    InternetShortcutAdapter,
    LegacyOfficeAdapter,
    MediaAdapter,
)


class UnsupportedFormatError(ValueError):
    pass


class FormatRouter:
    def __init__(
        self,
        adapters: dict[str, FormatAdapter] | None = None,
        *,
        docling_document_timeout_seconds: float = 300.0,
        docling_artifacts_path: Path | None = None,
        legacy_cache_root: Path = Path("storage/cache/legacy"),
        libreoffice_path: Path | None = None,
        legacy_conversion_timeout_seconds: float = 180.0,
    ) -> None:
        if adapters is None:
            docling = DoclingAdapter(
                docling_document_timeout_seconds,
                artifacts_path=docling_artifacts_path,
            )
            image = ImageAdapter()
            legacy = LegacyOfficeAdapter(
                docling,
                legacy_cache_root,
                libreoffice_path=libreoffice_path,
                conversion_timeout_seconds=legacy_conversion_timeout_seconds,
            )
            adapters = {
                ".pdf": docling,
                ".docx": docling,
                ".doc": legacy,
                ".rtf": legacy,
                ".xlsx": ExcelAdapter(),
                ".jpg": image,
                ".jpeg": image,
                ".png": image,
                ".mp4": MediaAdapter(),
                ".url": InternetShortcutAdapter(),
            }
        self._adapters = {extension.casefold(): adapter for extension, adapter in adapters.items()}

    @property
    def supported_extensions(self) -> frozenset[str]:
        return frozenset(self._adapters)

    def adapter_for(self, path: Path | str) -> FormatAdapter:
        extension = Path(path).suffix.casefold()
        try:
            return self._adapters[extension]
        except KeyError as exc:
            raise UnsupportedFormatError(f"No adapter registered for extension {extension or '<none>'}") from exc
