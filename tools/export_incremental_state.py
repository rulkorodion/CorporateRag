from __future__ import annotations

"""Create a consistent one-time Stage 11 registry/OCR-cache bootstrap archive."""

import argparse
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage", type=Path, default=Path("storage"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tmp/corporate-rag-incremental-state.tar.gz"),
    )
    parser.add_argument("--without-ocr-cache", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    storage = args.storage.resolve()
    source_db = storage / "document_registry.db"
    if not source_db.is_file():
        raise FileNotFoundError(source_db)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="corporate-rag-stage11-") as temporary:
        stage = Path(temporary) / "storage"
        stage.mkdir(parents=True)
        backup_db = stage / "document_registry.db"
        with closing(sqlite3.connect(source_db)) as source, closing(
            sqlite3.connect(backup_db)
        ) as target:
            source.backup(target)
        with closing(sqlite3.connect(backup_db)) as connection:
            documents = int(connection.execute("SELECT count(*) FROM document").fetchone()[0])
            versions = int(
                connection.execute("SELECT count(*) FROM document_version").fetchone()[0]
            )
        if documents == 0 or versions == 0:
            raise RuntimeError("registry backup is empty")

        ocr_files = 0
        ocr_pages = storage / "ocr" / "pages"
        if ocr_pages.is_dir() and not args.without_ocr_cache:
            destination = stage / "ocr" / "pages"
            shutil.copytree(ocr_pages, destination)
            ocr_files = sum(1 for item in destination.rglob("*") if item.is_file())

        with tarfile.open(args.output, "w:gz") as archive:
            archive.add(stage, arcname="storage")
    print(f"archive={args.output.resolve()}")
    print(f"documents={documents}")
    print(f"versions={versions}")
    print(f"ocr_cache_files={ocr_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
