from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from corporate_rag_ingestion.db import create_schema, make_engine, make_session_factory


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(f"sqlite:///{(tmp_path / 'registry.db').as_posix()}")
    create_schema(engine)
    return make_session_factory(engine)

