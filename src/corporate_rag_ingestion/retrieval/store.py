from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient, models

from .models import SparseEmbedding
from .settings import RetrievalSettings


INCREMENTAL_CANDIDATE_FIELD = "_incremental_candidate"


@dataclass(slots=True)
class DocumentSwitch:
    document_id: str
    new_point_ids: list[str]
    previous_points: list[models.Record]


class QdrantStore:
    def __init__(self, settings: RetrievalSettings) -> None:
        self.settings = settings
        if settings.qdrant_url:
            self.client = QdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key,
                timeout=120,
            )
            self.local_path: Path | None = None
        else:
            settings.qdrant_path.mkdir(parents=True, exist_ok=True)
            self.client = QdrantClient(path=str(settings.qdrant_path))
            self.local_path = settings.qdrant_path

    def close(self) -> None:
        self.client.close()

    def ensure_collection(self) -> bool:
        name = self.settings.collection_name
        if self.client.collection_exists(name):
            info = self.client.get_collection(name)
            vectors = info.config.params.vectors
            dense = vectors.get(self.settings.dense_vector_name) if isinstance(vectors, dict) else None
            if dense is None or dense.size != self.settings.dense_dimension:
                raise RuntimeError(
                    "Qdrant collection dense-vector configuration is incompatible; "
                    "use a new collection name or recreate it explicitly"
                )
            sparse = info.config.params.sparse_vectors or {}
            if self.settings.sparse_vector_name not in sparse:
                raise RuntimeError("Qdrant collection has no configured BM25 sparse vector")
            metadata = info.config.metadata or {}
            schema = metadata.get("index_schema_version")
            if schema is not None and schema != self.settings.index_schema_version:
                raise RuntimeError(
                    "Qdrant collection index schema is incompatible; "
                    "use a new collection name or recreate it explicitly"
                )
            return False
        self.client.create_collection(
            collection_name=name,
            vectors_config={
                self.settings.dense_vector_name: models.VectorParams(
                    size=self.settings.dense_dimension,
                    distance=models.Distance.COSINE,
                )
            },
            sparse_vectors_config={
                self.settings.sparse_vector_name: models.SparseVectorParams(
                    modifier=models.Modifier.IDF,
                    index=models.SparseIndexParams(on_disk=False),
                )
            },
            on_disk_payload=True,
            metadata={
                "index_schema_version": self.settings.index_schema_version,
                "index_settings_hash": self.settings.index_settings_hash,
            },
        )
        return True

    def set_index_metadata(self) -> None:
        self.client.update_collection(
            collection_name=self.settings.collection_name,
            metadata={
                "index_schema_version": self.settings.index_schema_version,
                "index_settings_hash": self.settings.index_settings_hash,
            },
        )

    def payloads(self) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        offset = None
        while True:
            records, offset = self.client.scroll(
                collection_name=self.settings.collection_name,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for record in records:
                output[str(record.id)] = dict(record.payload or {})
            if offset is None:
                break
        return output

    def records_for_document(
        self,
        document_id: str,
        *,
        with_vectors: bool = False,
    ) -> list[models.Record]:
        records_out: list[models.Record] = []
        offset = None
        query_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchValue(value=document_id),
                )
            ]
        )
        while True:
            records, offset = self.client.scroll(
                collection_name=self.settings.collection_name,
                scroll_filter=query_filter,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=with_vectors,
            )
            records_out.extend(records)
            if offset is None:
                break
        return records_out

    def payloads_for_document(self, document_id: str) -> dict[str, dict[str, Any]]:
        return {
            str(record.id): dict(record.payload or {})
            for record in self.records_for_document(document_id)
        }

    def controlled_replace_document(
        self,
        document_id: str,
        version_id: str,
        records: list[tuple[str, list[float], SparseEmbedding, dict[str, Any]]],
    ) -> DocumentSwitch:
        """Install one complete document version while keeping rollback data.

        New points are fully upserted and verified before points belonging to older
        versions of the same document are removed. No other document can be touched.
        """

        for point_id, _dense, _sparse, payload in records:
            if payload.get("document_id") != document_id or payload.get("version_id") != version_id:
                raise ValueError(f"point {point_id} does not belong to candidate document/version")

        existing = self.records_for_document(document_id, with_vectors=True)
        previous = [
            record
            for record in existing
            if str((record.payload or {}).get("version_id")) != version_id
        ]
        new_ids = [point_id for point_id, *_rest in records]
        previous_ids = {str(record.id) for record in previous}
        collisions = previous_ids.intersection(new_ids)
        if collisions:
            raise RuntimeError(f"candidate chunk IDs collide with active points: {sorted(collisions)}")

        switch = DocumentSwitch(
            document_id=document_id,
            new_point_ids=new_ids,
            previous_points=previous,
        )
        staged_records = [
            (
                point_id,
                dense,
                sparse,
                {**payload, INCREMENTAL_CANDIDATE_FIELD: True},
            )
            for point_id, dense, sparse, payload in records
        ]
        try:
            for start in range(0, len(staged_records), self.settings.upsert_batch_size):
                self.upsert(staged_records[start : start + self.settings.upsert_batch_size])
            candidate_payloads = {
                point_id: payload
                for point_id, payload in self.payloads_for_document(document_id).items()
                if str(payload.get("version_id")) == version_id
            }
            if set(candidate_payloads) != set(new_ids):
                raise RuntimeError(
                    "candidate Qdrant verification failed: "
                    f"expected={len(new_ids)} actual={len(candidate_payloads)}"
                )
            if any(
                payload.get(INCREMENTAL_CANDIDATE_FIELD) is not True
                for payload in candidate_payloads.values()
            ):
                raise RuntimeError("candidate Qdrant staging flag verification failed")
            if new_ids:
                self.client.set_payload(
                    collection_name=self.settings.collection_name,
                    payload={INCREMENTAL_CANDIDATE_FIELD: False},
                    points=new_ids,
                    wait=True,
                )
                active_payloads = {
                    point_id: payload
                    for point_id, payload in self.payloads_for_document(document_id).items()
                    if str(payload.get("version_id")) == version_id
                }
                if set(active_payloads) != set(new_ids) or any(
                    payload.get(INCREMENTAL_CANDIDATE_FIELD) is not False
                    for payload in active_payloads.values()
                ):
                    raise RuntimeError("candidate Qdrant activation verification failed")
            self.delete(sorted(previous_ids))
            return switch
        except Exception:
            self.rollback_document_switch(switch)
            raise

    def rollback_document_switch(self, switch: DocumentSwitch) -> None:
        self.delete(switch.new_point_ids)
        if not switch.previous_points:
            return
        points = [
            models.PointStruct(
                id=record.id,
                vector=record.vector or {},
                payload=dict(record.payload or {}),
            )
            for record in switch.previous_points
        ]
        self.client.upsert(
            collection_name=self.settings.collection_name,
            points=points,
            wait=True,
        )

    def remove_document(self, document_id: str) -> DocumentSwitch:
        previous = self.records_for_document(document_id, with_vectors=True)
        switch = DocumentSwitch(
            document_id=document_id,
            new_point_ids=[],
            previous_points=previous,
        )
        self.delete([str(record.id) for record in previous])
        return switch

    def upsert(
        self,
        records: list[tuple[str, list[float], SparseEmbedding, dict[str, Any]]],
    ) -> None:
        points = [
            models.PointStruct(
                id=point_id,
                vector={
                    self.settings.dense_vector_name: dense,
                    self.settings.sparse_vector_name: models.SparseVector(
                        indices=sparse.indices,
                        values=sparse.values,
                    ),
                },
                payload=payload,
            )
            for point_id, dense, sparse, payload in records
        ]
        self.client.upsert(
            collection_name=self.settings.collection_name,
            points=points,
            wait=True,
        )

    def delete(self, point_ids: list[str]) -> None:
        if not point_ids:
            return
        self.client.delete(
            collection_name=self.settings.collection_name,
            points_selector=models.PointIdsList(points=point_ids),
            wait=True,
        )

    def count(self) -> int:
        return int(
            self.client.count(
                collection_name=self.settings.collection_name,
                exact=True,
            ).count
        )

    def query_dense(
        self,
        vector: list[float],
        *,
        limit: int,
        query_filter: models.Filter | None = None,
    ):  # type: ignore[no-untyped-def]
        return self.client.query_points(
            collection_name=self.settings.collection_name,
            query=vector,
            using=self.settings.dense_vector_name,
            query_filter=self._without_incremental_candidates(query_filter),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        ).points

    def query_sparse(
        self,
        vector: SparseEmbedding,
        *,
        limit: int,
        query_filter: models.Filter | None = None,
    ):  # type: ignore[no-untyped-def]
        return self.client.query_points(
            collection_name=self.settings.collection_name,
            query=models.SparseVector(indices=vector.indices, values=vector.values),
            using=self.settings.sparse_vector_name,
            query_filter=self._without_incremental_candidates(query_filter),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        ).points

    @staticmethod
    def _without_incremental_candidates(
        query_filter: models.Filter | None,
    ) -> models.Filter:
        hidden = models.FieldCondition(
            key=INCREMENTAL_CANDIDATE_FIELD,
            match=models.MatchValue(value=True),
        )
        if query_filter is None:
            return models.Filter(must_not=[hidden])
        return models.Filter(
            should=query_filter.should,
            min_should=query_filter.min_should,
            must=query_filter.must,
            must_not=[*(query_filter.must_not or []), hidden],
        )

    def storage_bytes(self) -> int:
        if self.local_path is None or not self.local_path.exists():
            return 0
        return sum(path.stat().st_size for path in self.local_path.rglob("*") if path.is_file())
