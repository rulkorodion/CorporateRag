from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient

from corporate_rag_ingestion.api.app import create_app
from corporate_rag_ingestion.api.settings import APISettings
from corporate_rag_ingestion.config import Settings
from corporate_rag_ingestion.schemas import NormalizedAsset, NormalizedDocument, NormalizedPage


JWT_SECRET = "viewer-test-secret"


class ViewerRuntime:
    def __init__(self, project_settings: Settings) -> None:
        self.project_settings = project_settings

    def warmup(self) -> None:
        pass

    def readiness(self):  # type: ignore[no-untyped-def]
        return {"ready": True}

    def close(self) -> None:
        pass


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write_document(
    project: Settings,
    *,
    document_id: str,
    version_id: str,
    relative_path: str,
    content: bytes,
    pages: int = 0,
    assets: list[NormalizedAsset] | None = None,
) -> Path:
    source = project.knowledge_base_root / relative_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    normalized = NormalizedDocument(
        document_id=document_id,
        version_id=version_id,
        metadata={"relative_path": relative_path, "sha256": sha256(content)},
        pages=[NormalizedPage(page_number=index + 1) for index in range(pages)],
        assets=assets or [],
    )
    target = project.normalized_root / document_id / f"{version_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(normalized.model_dump_json(), encoding="utf-8")
    return source


def viewer_client(tmp_path: Path, *, secret: str = JWT_SECRET) -> tuple[TestClient, Settings]:
    project = Settings(
        _env_file=None,
        knowledge_base_path=tmp_path / "knowledge-base",
        storage_path=tmp_path / "storage",
    )
    runtime = ViewerRuntime(project)
    app = create_app(
        settings=APISettings(
            _env_file=None,
            warmup_on_startup=False,
            onlyoffice_public_url="/onlyoffice",
            onlyoffice_internal_file_base_url="http://host.docker.internal:3001",
            onlyoffice_jwt_secret=secret,
        ),
        runtime_factory=lambda _: runtime,  # type: ignore[arg-type]
    )
    return TestClient(app), project


def decode_and_verify(token: str) -> dict[str, object]:
    header, payload, signature = token.split(".")
    expected = hmac.new(JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    assert hmac.compare_digest(actual, expected)
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def test_pdf_metadata_original_and_signed_onlyoffice_config(tmp_path: Path) -> None:
    client, project = viewer_client(tmp_path)
    content = b"%PDF-1.7\nviewer"
    write_document(
        project,
        document_id="doc-pdf",
        version_id="version-1",
        relative_path="manuals/manual.pdf",
        content=content,
        pages=4,
    )
    with client:
        metadata = client.get("/api/viewer/documents/doc-pdf/version-1/metadata")
        opened = client.get("/api/viewer/documents/doc-pdf/version-1/content")
        configured = client.get("/api/viewer/documents/doc-pdf/version-1/onlyoffice-config")
    assert metadata.json()["viewer_kind"] == "pdf"
    assert metadata.json()["page_count"] == 4
    assert opened.content == content
    body = configured.json()
    assert body["document_server_url"] == "/onlyoffice"
    config = body["config"]
    assert config["documentType"] == "pdf"
    assert config["editorConfig"]["mode"] == "view"
    assert config["document"]["permissions"]["edit"] is False
    assert config["document"]["permissions"]["download"] is False
    assert config["editorConfig"]["customization"]["anonymous"] == {
        "request": False
    }
    assert config["document"]["url"] == "http://host.docker.internal:3001/api/viewer/documents/doc-pdf/version-1/content"
    signed = decode_and_verify(config["token"])
    assert signed["document"] == config["document"]
    assert signed["editorConfig"] == config["editorConfig"]


def test_docx_and_xlsx_use_onlyoffice_without_preview_conversion(tmp_path: Path) -> None:
    client, project = viewer_client(tmp_path)
    write_document(project, document_id="doc", version_id="v1", relative_path="office/rules.docx", content=b"docx")
    write_document(project, document_id="sheet", version_id="v1", relative_path="tables/report.xlsx", content=b"xlsx")
    with client:
        doc = client.get("/api/viewer/documents/doc/v1/onlyoffice-config").json()["config"]
        sheet = client.get("/api/viewer/documents/sheet/v1/onlyoffice-config").json()["config"]
    assert doc["documentType"] == "word"
    assert doc["document"]["fileType"] == "docx"
    assert sheet["documentType"] == "cell"
    assert sheet["document"]["fileType"] == "xlsx"
    assert not (project.storage_root / "preview-cache").exists()


def test_new_version_gets_new_onlyoffice_document_key(tmp_path: Path) -> None:
    client, project = viewer_client(tmp_path)
    write_document(project, document_id="doc", version_id="v1", relative_path="source.docx", content=b"first")
    write_document(project, document_id="doc", version_id="v2", relative_path="source.docx", content=b"second")
    with client:
        first = client.get("/api/viewer/documents/doc/v1/onlyoffice-config").json()["config"]["document"]["key"]
        second = client.get("/api/viewer/documents/doc/v2/onlyoffice-config").json()["config"]["document"]["key"]
    assert first == sha256(b"first")
    assert second == sha256(b"second")
    assert first != second


def test_image_is_served_by_safe_asset_id(tmp_path: Path) -> None:
    client, project = viewer_client(tmp_path)
    image_path = project.storage_root / "assets" / "image-doc" / "image.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image = b"\x89PNG\r\n\x1a\n"
    image_path.write_bytes(image)
    write_document(
        project,
        document_id="image-doc",
        version_id="image-v1",
        relative_path="images/original.png",
        content=image,
        assets=[NormalizedAsset(asset_id="asset-1", type="image", local_path="assets/image-doc/image.png")],
    )
    with client:
        metadata = client.get("/api/viewer/assets/image-doc/image-v1/asset-1/metadata")
        content = client.get("/api/viewer/assets/image-doc/image-v1/asset-1/content")
    assert metadata.json()["viewer_kind"] == "image"
    assert content.content == image


def test_mp4_supports_http_range_and_seek(tmp_path: Path) -> None:
    client, project = viewer_client(tmp_path)
    video = b"0123456789abcdef"
    write_document(project, document_id="video-doc", version_id="video-v1", relative_path="video/demo.mp4", content=video)
    with client:
        response = client.get("/api/viewer/documents/video-doc/video-v1/content", headers={"Range": "bytes=3-7"})
    assert response.status_code == 206
    assert response.content == b"34567"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"] == "bytes 3-7/16"


def test_missing_secret_returns_service_unavailable(tmp_path: Path) -> None:
    client, project = viewer_client(tmp_path, secret="")
    write_document(project, document_id="doc", version_id="v1", relative_path="source.docx", content=b"docx")
    with client:
        response = client.get("/api/viewer/documents/doc/v1/onlyoffice-config")
    assert response.status_code == 503


def test_missing_unsupported_traversal_and_arbitrary_access_are_rejected(tmp_path: Path) -> None:
    client, project = viewer_client(tmp_path)
    write_document(project, document_id="shortcut", version_id="v1", relative_path="links/source.url", content=b"[InternetShortcut]")
    (project.knowledge_base_root / "private.pdf").write_bytes(b"%PDF-private")
    with client:
        missing = client.get("/api/viewer/documents/missing/v1/metadata")
        unsupported = client.get("/api/viewer/documents/shortcut/v1/metadata")
        traversal = client.get("/api/viewer/documents/..%2Foutside/v1/metadata")
        arbitrary = client.get("/api/viewer/documents/private/v1/content")
        old_path_api = client.get("/api/files/source", params={"path": "private.pdf"})
    assert missing.status_code == 404
    assert unsupported.status_code == 415
    assert traversal.status_code == 404
    assert arbitrary.status_code == 404
    assert old_path_api.status_code == 404
