from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


UpdateState = Literal["new", "changed", "unchanged", "deleted"]


@dataclass(frozen=True, slots=True)
class PlannedFile:
    relative_path: str
    state: UpdateState
    document_id: str | None = None
    active_version_id: str | None = None
    candidate_sha256: str | None = None
    forced: bool = False


@dataclass(slots=True)
class IncrementalPlan:
    scanned: int = 0
    new: list[PlannedFile] = field(default_factory=list)
    changed: list[PlannedFile] = field(default_factory=list)
    deleted: list[PlannedFile] = field(default_factory=list)
    unchanged: list[PlannedFile] = field(default_factory=list)
    unmatched_force: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "new": [asdict(item) for item in self.new],
            "changed": [asdict(item) for item in self.changed],
            "deleted": [asdict(item) for item in self.deleted],
            "unchanged": [asdict(item) for item in self.unchanged],
            "counts": {
                "new": len(self.new),
                "changed": len(self.changed),
                "deleted": len(self.deleted),
                "unchanged": len(self.unchanged),
            },
            "unmatched_force": self.unmatched_force,
        }


@dataclass(slots=True)
class FileUpdateResult:
    relative_path: str
    state: str
    status: Literal["updated", "deleted", "unchanged", "failed", "dry_run"]
    document_id: str | None = None
    previous_version_id: str | None = None
    version_id: str | None = None
    normalized: bool = False
    ocr_pages_processed: int = 0
    ocr_pages_reused: int = 0
    chunks_created: int = 0
    chunks_removed: int = 0
    embeddings_calculated: int = 0
    qdrant_points_added: int = 0
    qdrant_points_removed: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None


@dataclass(slots=True)
class IncrementalUpdateReport:
    dry_run: bool = False
    scanned: int = 0
    new: int = 0
    changed: int = 0
    deleted: int = 0
    unchanged: int = 0
    normalized: int = 0
    ocr_pages_processed: int = 0
    ocr_pages_reused: int = 0
    chunks_created: int = 0
    chunks_removed: int = 0
    embeddings_calculated: int = 0
    embeddings_reused: int = 0
    qdrant_points_added: int = 0
    qdrant_points_removed: int = 0
    errors: list[str] = field(default_factory=list)
    files: list[FileUpdateResult] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    report_path: str | None = None

    def add_file(self, result: FileUpdateResult) -> None:
        self.files.append(result)
        self.normalized += int(result.normalized)
        self.ocr_pages_processed += result.ocr_pages_processed
        self.ocr_pages_reused += result.ocr_pages_reused
        self.chunks_created += result.chunks_created
        self.chunks_removed += result.chunks_removed
        self.embeddings_calculated += result.embeddings_calculated
        self.qdrant_points_added += result.qdrant_points_added
        self.qdrant_points_removed += result.qdrant_points_removed
        if result.error:
            self.errors.append(f"{result.relative_path}: {result.error}")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["error_count"] = len(self.errors)
        return payload
