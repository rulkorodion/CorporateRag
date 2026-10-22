from pathlib import Path

import pytest

from corporate_rag_ingestion.adapters import (
    DoclingAdapter,
    ExcelAdapter,
    ImageAdapter,
    InternetShortcutAdapter,
    LegacyOfficeAdapter,
    MediaAdapter,
)
from corporate_rag_ingestion.router import FormatRouter, UnsupportedFormatError
from corporate_rag_ingestion.config import Settings


@pytest.mark.parametrize(
    ("name", "adapter_type"),
    [
        ("a.pdf", DoclingAdapter),
        ("a.DOCX", DoclingAdapter),
        ("a.doc", LegacyOfficeAdapter),
        ("a.rtf", LegacyOfficeAdapter),
        ("a.xlsx", ExcelAdapter),
        ("a.jpg", ImageAdapter),
        ("a.JPEG", ImageAdapter),
        ("a.png", ImageAdapter),
        ("a.mp4", MediaAdapter),
        ("a.url", InternetShortcutAdapter),
    ],
)
def test_router_dispatches_to_adapter(name: str, adapter_type: type) -> None:
    assert isinstance(FormatRouter().adapter_for(Path(name)), adapter_type)


def test_router_rejects_unknown_format() -> None:
    with pytest.raises(UnsupportedFormatError):
        FormatRouter().adapter_for("unknown.odt")


def test_docling_offline_artifacts_path_is_configurable(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, docling_artifacts_path=tmp_path)
    router = FormatRouter(docling_artifacts_path=settings.docling_artifacts_root)

    adapter = router.adapter_for("document.pdf")

    assert isinstance(adapter, DoclingAdapter)
    assert adapter._artifacts_path == tmp_path
