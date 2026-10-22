from __future__ import annotations

import uuid

from ..schemas import NormalizedAsset
from .base import AdapterContext, FormatAdapter


class MediaAdapter(FormatAdapter):
    """Registers media as a non-indexable asset; it never transcribes video."""

    def normalize(self, context: AdapterContext):  # type: ignore[no-untyped-def]
        asset_id = str(uuid.uuid5(uuid.UUID(context.document_id), context.version_id))
        source_relative = context.source_path.relative_to(context.knowledge_base_root).as_posix()
        return context.normalized_document(
            metadata={
                **context.metadata,
                "role": "media_asset",
                "indexable": False,
                "transcript_status": context.metadata.get("transcript_status", "missing"),
            },
            assets=[
                NormalizedAsset(
                    asset_id=asset_id,
                    type="video",
                    local_path=source_relative,
                    visual_role="media_asset",
                    visual_classification_status="not_requested",
                    metadata={"path_scope": "knowledge_base"},
                )
            ],
        )
