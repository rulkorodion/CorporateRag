from __future__ import annotations

import configparser
from urllib.parse import urlparse

from .base import AdapterContext, FormatAdapter


def decode_shortcut(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1251", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Unable to decode Internet Shortcut")


def is_google_sheet(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    return host in {"docs.google.com", "sheets.google.com"} and "/spreadsheets/" in parsed.path


class InternetShortcutAdapter(FormatAdapter):
    def normalize(self, context: AdapterContext):  # type: ignore[no-untyped-def]
        text = decode_shortcut(context.source_path.read_bytes())
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read_string(text)
        if not parser.has_section("InternetShortcut"):
            raise ValueError(f"Missing [InternetShortcut] section: {context.source_path}")
        url = parser.get("InternetShortcut", "URL", fallback="").strip()
        if not url:
            raise ValueError(f"Missing URL in Internet Shortcut: {context.source_path}")
        sheet = is_google_sheet(url)
        return context.normalized_document(
            metadata={
                **context.metadata,
                "url": url,
                "external_resource_type": "external_google_sheet" if sheet else "internet_shortcut",
                "download_status": "not_requested",
                "indexable": False,
            }
        )

