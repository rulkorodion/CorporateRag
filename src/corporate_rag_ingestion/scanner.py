from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".rtf", ".xlsx",
    ".jpg", ".jpeg", ".png", ".mp4", ".url",
}
IGNORED_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
IGNORED_SUFFIXES = {".tmp", ".part", ".crdownload"}


@dataclass(frozen=True, slots=True)
class ScannedFile:
    absolute_path: Path
    relative_path: str
    file_name: str
    extension: str
    folder_path: tuple[str, ...]
    size_bytes: int
    modified_at: datetime
    sha256: str
    supported: bool


def should_ignore(path: Path) -> bool:
    name = path.name.casefold()
    return (
        name in IGNORED_NAMES
        or name.startswith("~$")
        or name.startswith(".~lock.")
        or path.suffix.casefold() in IGNORED_SUFFIXES
    )


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class FileScanner:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def scan(self) -> list[ScannedFile]:
        if not self.root.exists():
            raise FileNotFoundError(f"Knowledge-base directory does not exist: {self.root}")
        if not self.root.is_dir():
            raise NotADirectoryError(f"Knowledge-base path is not a directory: {self.root}")

        paths = sorted(
            (path for path in self.root.rglob("*") if path.is_file() and not should_ignore(path)),
            key=lambda item: item.relative_to(self.root).as_posix().casefold(),
        )
        result: list[ScannedFile] = []
        for path in paths:
            result.append(self._scan_path(path))
        return result

    def _scan_path(self, path: Path) -> ScannedFile:
        # Avoid registering a hash paired with stale size/mtime if a producer is
        # still writing the file while a scan is in progress.
        for _attempt in range(2):
            before = path.stat()
            digest = sha256_file(path)
            after = path.stat()
            if before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns:
                relative = path.relative_to(self.root)
                extension = path.suffix.casefold()
                return ScannedFile(
                    absolute_path=path,
                    relative_path=relative.as_posix(),
                    file_name=path.name,
                    extension=extension,
                    folder_path=(
                        tuple(relative.parent.parts) if relative.parent != Path(".") else ()
                    ),
                    size_bytes=after.st_size,
                    modified_at=datetime.fromtimestamp(after.st_mtime, tz=timezone.utc),
                    sha256=digest,
                    supported=extension in SUPPORTED_EXTENSIONS,
                )
        raise RuntimeError(f"File changed while scanning: {path}")
