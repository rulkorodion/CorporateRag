from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import PROJECT_ROOT


class Base(DeclarativeBase):
    pass


def resolve_database_url(database_url: str) -> str:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url.startswith("sqlite:////"):
        return database_url
    relative = database_url[len(prefix) :]
    db_path = Path(relative)
    if db_path.is_absolute():
        return database_url
    absolute = (PROJECT_ROOT / db_path).resolve()
    absolute.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{absolute.as_posix()}"


def make_engine(database_url: str) -> Engine:
    resolved = resolve_database_url(database_url)
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if resolved.startswith("sqlite:"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(resolved, **kwargs)
    if resolved.startswith("sqlite:"):
        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_schema(engine: Engine) -> None:
    from . import registry_models  # noqa: F401

    Base.metadata.create_all(engine)
