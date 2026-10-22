from __future__ import annotations

import hashlib
from pathlib import Path

from corporate_rag_ingestion.scanner import FileScanner, sha256_file


def test_recursive_scanner_preserves_structure_and_ignores_temporary_files(tmp_path: Path) -> None:
    nested = tmp_path / "department" / "level-2" / "level-3"
    nested.mkdir(parents=True)
    source = nested / "guide.PDF"
    source.write_bytes(b"content")
    (nested / "~$guide.docx").write_bytes(b"temporary")
    (nested / "Thumbs.db").write_bytes(b"system")

    files = FileScanner(tmp_path).scan()

    assert len(files) == 1
    assert files[0].relative_path == "department/level-2/level-3/guide.PDF"
    assert files[0].folder_path == ("department", "level-2", "level-3")
    assert files[0].extension == ".pdf"
    assert files[0].supported is True


def test_sha256_is_streamed_and_correct(tmp_path: Path) -> None:
    source = tmp_path / "large.bin"
    content = b"abc123" * 400_000
    source.write_bytes(content)

    assert sha256_file(source, chunk_size=17) == hashlib.sha256(content).hexdigest()

