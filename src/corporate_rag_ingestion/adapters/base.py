from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..schemas import NormalizedDocument


@dataclass(frozen=True, slots=True)
class AdapterContext:
    document_id: str
    version_id: str
    source_path: Path
    knowledge_base_root: Path
    storage_root: Path
    assets_directory: Path
    metadata: dict[str, Any]

    def normalized_document(self, **kwargs: Any) -> NormalizedDocument:
        metadata = kwargs.pop("metadata", self.metadata)
        return NormalizedDocument(
            document_id=self.document_id,
            version_id=self.version_id,
            metadata=metadata,
            **kwargs,
        )

    def storage_relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.storage_root.resolve()).as_posix()
        except ValueError:
            return str(path.resolve())


class FormatAdapter(ABC):
    @abstractmethod
    def normalize(self, context: AdapterContext) -> NormalizedDocument:
        raise NotImplementedError
