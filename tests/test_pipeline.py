from __future__ import annotations

from pathlib import Path

from PIL import Image
from sqlalchemy.orm import Session, sessionmaker

from corporate_rag_ingestion.config import Settings
from corporate_rag_ingestion.pipeline import IngestionPipeline


def test_scan_then_ingest_normalizes_registered_current_version(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    kb = tmp_path / "kb"
    storage = tmp_path / "storage"
    kb.mkdir()
    Image.new("RGB", (8, 9), "blue").save(kb / "image.png")
    settings = Settings(
        knowledge_base_path=kb,
        storage_path=storage,
        database_url=f"sqlite:///{(tmp_path / 'unused.db').as_posix()}",
    )
    pipeline = IngestionPipeline(settings, session_factory)

    scan_summary = pipeline.run(scan_only=True)
    ingest_summary = pipeline.run()
    third_summary = pipeline.run()

    assert scan_summary.new == 1
    assert scan_summary.normalized == 0
    assert ingest_summary.unchanged == 1
    assert ingest_summary.normalized == 1
    assert third_summary.unchanged == 1
    assert third_summary.normalized == 0
    assert len(list((storage / "normalized").rglob("*.json"))) == 1

