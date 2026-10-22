from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import FileResponse, StreamingResponse

from ..config import Settings
from ..schemas import NormalizedDocument
from ..ui.files import mime_type, safe_asset_path, safe_source_path
from .settings import APISettings


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_PDF = frozenset({".pdf"})
_OFFICE = frozenset({".docx", ".doc", ".rtf"})
_SHEET = frozenset({".xlsx", ".xls"})
_IMAGE = frozenset({".png", ".jpg", ".jpeg"})
_VIDEO = frozenset({".mp4"})


class ViewerNotFoundError(LookupError):
    pass


class ViewerUnsupportedError(ValueError):
    pass


class OnlyOfficeConfigurationError(RuntimeError):
    pass


ViewerKind = Literal["pdf", "office", "spreadsheet", "image", "video"]


@dataclass(frozen=True)
class ViewerDocument:
    document_id: str
    version_id: str
    source_path: Path
    relative_path: str
    sha256: str
    kind: ViewerKind
    document: NormalizedDocument

    def metadata(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "version_id": self.version_id,
            "file_name": self.source_path.name,
            "file_type": self.source_path.suffix.casefold().lstrip("."),
            "media_type": mime_type(self.source_path),
            "viewer_kind": self.kind,
            "sha256": self.sha256,
            "page_count": len(self.document.pages) or None,
        }


@dataclass(frozen=True)
class ViewerAsset:
    asset_id: str
    document_id: str
    version_id: str
    path: Path
    source_page: int | None

    def metadata(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "document_id": self.document_id,
            "version_id": self.version_id,
            "file_name": self.path.name,
            "file_type": self.path.suffix.casefold().lstrip("."),
            "media_type": mime_type(self.path),
            "viewer_kind": "image",
            "source_page": self.source_page,
        }


class DocumentViewerService:
    """Resolve files by normalized IDs and build signed ONLYOFFICE configs."""

    def __init__(self, project: Settings, api: APISettings) -> None:
        self.knowledge_base_root = project.knowledge_base_root.resolve()
        self.storage_root = project.storage_root.resolve()
        self.normalized_root = getattr(
            project, "normalized_root", self.storage_root / "normalized"
        ).resolve()
        self.onlyoffice_public_url = api.onlyoffice_public_url.rstrip("/")
        self.onlyoffice_internal_file_base_url = (
            api.onlyoffice_internal_file_base_url.rstrip("/")
        )
        self.onlyoffice_jwt_secret = api.onlyoffice_jwt_secret

    @staticmethod
    def _validate_id(value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ViewerNotFoundError("Viewer resource not found")
        return value

    def normalized_document(
        self, document_id: str, version_id: str
    ) -> NormalizedDocument:
        document_id = self._validate_id(document_id)
        version_id = self._validate_id(version_id)
        path = (self.normalized_root / document_id / f"{version_id}.json").resolve()
        if not path.is_relative_to(self.normalized_root) or not path.is_file():
            raise ViewerNotFoundError("Document version not found")
        try:
            document = NormalizedDocument.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ViewerNotFoundError("Document version not found") from exc
        if document.document_id != document_id or document.version_id != version_id:
            raise ViewerNotFoundError("Document version not found")
        return document

    def document(self, document_id: str, version_id: str) -> ViewerDocument:
        document = self.normalized_document(document_id, version_id)
        relative_path = document.metadata.get("relative_path")
        if not isinstance(relative_path, str):
            raise ViewerNotFoundError("Source file not found")
        source_path = safe_source_path(self.knowledge_base_root, relative_path)
        if source_path is None:
            raise ViewerNotFoundError("Source file not found")
        extension = source_path.suffix.casefold()
        if extension in _PDF:
            kind: ViewerKind = "pdf"
        elif extension in _OFFICE:
            kind = "office"
        elif extension in _SHEET:
            kind = "spreadsheet"
        elif extension in _IMAGE:
            kind = "image"
        elif extension in _VIDEO:
            kind = "video"
        else:
            raise ViewerUnsupportedError(f"Unsupported viewer format: {extension}")
        stored_sha = str(document.metadata.get("sha256") or "").casefold()
        sha256 = stored_sha if _SHA256.fullmatch(stored_sha) else self._sha256(source_path)
        return ViewerDocument(
            document_id=document_id,
            version_id=version_id,
            source_path=source_path,
            relative_path=relative_path,
            sha256=sha256,
            kind=kind,
            document=document,
        )

    def asset(self, document_id: str, version_id: str, asset_id: str) -> ViewerAsset:
        asset_id = self._validate_id(asset_id)
        document = self.normalized_document(document_id, version_id)
        normalized_asset = next(
            (item for item in document.assets if item.asset_id == asset_id), None
        )
        if normalized_asset is None:
            raise ViewerNotFoundError("Image asset not found")
        scope = str(normalized_asset.metadata.get("path_scope") or "storage")
        if scope not in {"storage", "knowledge_base"}:
            raise ViewerNotFoundError("Image asset not found")
        path = safe_asset_path(
            knowledge_base_root=self.knowledge_base_root,
            storage_root=self.storage_root,
            relative_path=normalized_asset.local_path,
            scope=scope,
        )
        if path is None:
            raise ViewerNotFoundError("Image asset not found")
        return ViewerAsset(
            asset_id=asset_id,
            document_id=document_id,
            version_id=version_id,
            path=path,
            source_page=normalized_asset.source_page,
        )

    def onlyoffice_config(self, item: ViewerDocument) -> dict[str, object]:
        if item.kind in {"image", "video"}:
            raise ViewerUnsupportedError("This file type uses the media viewer")
        if not self.onlyoffice_jwt_secret:
            raise OnlyOfficeConfigurationError("ONLYOFFICE JWT secret is not configured")
        extension = item.source_path.suffix.casefold().lstrip(".")
        document_type = "cell" if item.kind == "spreadsheet" else "word"
        if item.kind == "pdf":
            document_type = "pdf"
        path = (
            f"/api/viewer/documents/{quote(item.document_id)}/"
            f"{quote(item.version_id)}/content"
        )
        config: dict[str, object] = {
            "documentType": document_type,
            "height": "100%",
            "width": "100%",
            "document": {
                "fileType": extension,
                "key": item.sha256[:64],
                "title": item.source_path.name,
                "url": f"{self.onlyoffice_internal_file_base_url}{path}",
                "permissions": {
                    "chat": False,
                    "comment": False,
                    "copy": True,
                    "download": False,
                    "edit": False,
                    "fillForms": False,
                    "print": True,
                    "review": False,
                },
            },
            "editorConfig": {
                "lang": "ru",
                "mode": "view",
                "coEditing": {"mode": "strict", "change": False},
                "customization": {
                    "anonymous": {"request": False},
                    "compactHeader": True,
                    "feedback": False,
                    "help": False,
                },
            },
        }
        config["token"] = self._jwt(config)
        return {
            "document_server_url": self.onlyoffice_public_url,
            "config": config,
        }

    def _jwt(self, payload: dict[str, object]) -> str:
        header = {"alg": "HS256", "typ": "JWT"}
        segments = [self._base64_json(header), self._base64_json(payload)]
        signing_input = ".".join(segments).encode("ascii")
        signature = hmac.new(
            self.onlyoffice_jwt_secret.encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
        return ".".join([*segments, self._base64(signature)])

    @staticmethod
    def _base64_json(value: object) -> str:
        return DocumentViewerService._base64(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )

    @staticmethod
    def _base64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()


def ranged_file_response(request: Request, path: Path, media_type: str):
    """Serve one RFC 7233 byte range for HTML5 video seeking."""

    size = path.stat().st_size
    range_header = request.headers.get("range")
    base_headers = {
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(path.name)}",
    }
    if not range_header:
        return FileResponse(path, media_type=media_type, headers=base_headers)
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if match is None or (not match.group(1) and not match.group(2)):
        return StreamingResponse(
            iter(()), status_code=416,
            headers={**base_headers, "Content-Range": f"bytes */{size}"},
        )
    if match.group(1):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
    else:
        suffix = int(match.group(2))
        start = max(0, size - suffix)
        end = size - 1
    if start >= size or end < start:
        return StreamingResponse(
            iter(()), status_code=416,
            headers={**base_headers, "Content-Range": f"bytes */{size}"},
        )
    end = min(end, size - 1)
    length = end - start + 1

    def body():  # type: ignore[no-untyped-def]
        remaining = length
        with path.open("rb") as file:
            file.seek(start)
            while remaining:
                chunk = file.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        body(), status_code=206, media_type=media_type,
        headers={
            **base_headers,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        },
    )
