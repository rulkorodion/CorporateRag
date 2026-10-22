from __future__ import annotations

import uuid
from pathlib import Path

from PIL import Image

from ..schemas import NormalizedAsset, NormalizedElement
from .base import AdapterContext, FormatAdapter


class ImageAdapter(FormatAdapter):
    def normalize(self, context: AdapterContext):  # type: ignore[no-untyped-def]
        with Image.open(context.source_path) as image:
            width, height = image.size
            image_metadata = {
                "width": width,
                "height": height,
                "format": image.format,
                "mode": image.mode,
            }
        asset_id = str(uuid.uuid5(uuid.UUID(context.document_id), context.version_id))
        source_relative = context.source_path.relative_to(context.knowledge_base_root).as_posix()
        asset = NormalizedAsset(
            asset_id=asset_id,
            type="image",
            local_path=source_relative,
            visual_role="standalone_visual",
            visual_classification_status="pending",
            metadata={**image_metadata, "path_scope": "knowledge_base"},
        )
        element = NormalizedElement(
            element_id=f"{context.version_id}:image:0",
            type="image",
            order=0,
            metadata={
                "asset_id": asset_id,
                "searchable_title": _searchable_title(context.source_path),
            },
        )
        return context.normalized_document(
            metadata={
                **context.metadata,
                **image_metadata,
                "visual_classification_status": "pending",
            },
            elements=[element],
            assets=[asset],
        )


def _searchable_title(path: Path) -> str:
    """Human-readable filename used only for locating a standalone visual asset."""

    return " ".join(path.stem.replace("_", " ").split())
