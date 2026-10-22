from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from corporate_rag_ingestion.adapters import AdapterContext
from corporate_rag_ingestion.adapters.legacy_office_adapter import (
    LegacyOfficeAdapter,
    LegacyOfficePrerequisiteError,
)


class StubDoclingAdapter:
    def __init__(self) -> None:
        self.received_path: Path | None = None

    def normalize(self, context: AdapterContext):  # type: ignore[no-untyped-def]
        self.received_path = context.source_path
        return context.normalized_document()


def make_context(source: Path, storage: Path) -> AdapterContext:
    return AdapterContext(
        document_id=str(uuid.uuid4()),
        version_id=str(uuid.uuid4()),
        source_path=source,
        knowledge_base_root=source.parent,
        storage_root=storage,
        assets_directory=storage / "assets",
        metadata={"extension": source.suffix},
    )


@pytest.mark.parametrize("extension", [".doc", ".rtf"])
def test_legacy_formats_convert_in_cache_then_use_docling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extension: str
) -> None:
    source = tmp_path / f"legacy{extension}"
    source.write_bytes(b"legacy")
    soffice = tmp_path / "soffice.exe"
    soffice.write_bytes(b"binary")
    stub = StubDoclingAdapter()
    captured_command: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_command.extend(command)
        output_directory = Path(command[command.index("--outdir") + 1])
        (output_directory / "source.docx").write_bytes(b"docx")
        return subprocess.CompletedProcess(command, 0, "converted", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = LegacyOfficeAdapter(  # type: ignore[arg-type]
        stub, tmp_path / "storage/cache/legacy", libreoffice_path=soffice
    )

    normalized = adapter.normalize(make_context(source, tmp_path / "storage"))

    assert stub.received_path is not None
    assert stub.received_path.suffix == ".docx"
    assert stub.received_path.is_file()
    assert stub.received_path.parent.parent == tmp_path / "storage/cache/legacy"
    assert len(stub.received_path.parent.name) == 24
    assert any(arg.startswith("-env:UserInstallation=file:") for arg in captured_command)
    assert normalized.metadata["legacy_conversion"]["source_format"] == extension


def test_missing_libreoffice_has_clear_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "legacy.rtf"
    source.write_bytes(b"legacy")
    monkeypatch.setattr(
        "corporate_rag_ingestion.adapters.legacy_office_adapter.find_soffice", lambda _path: None
    )
    adapter = LegacyOfficeAdapter(  # type: ignore[arg-type]
        StubDoclingAdapter(), tmp_path / "cache"
    )

    with pytest.raises(LegacyOfficePrerequisiteError, match="LIBREOFFICE_PATH"):
        adapter.normalize(make_context(source, tmp_path / "storage"))
