"""Database engine, session factory and schema creation."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        # check_same_thread: connections are shared across request threads.
        # timeout: busy timeout while waiting for another writer's lock.
        return {"connect_args": {"timeout": 30, "check_same_thread": False}}
    return {"pool_pre_ping": True}


engine = create_engine(settings.DATABASE_URL, future=True, **_engine_kwargs(settings.DATABASE_URL))


if settings.DATABASE_URL.startswith("sqlite"):
    # The pysqlite driver in its default mode second-guesses transaction
    # control: it does not BEGIN before SAVEPOINT, so the outermost
    # RELEASE SAVEPOINT silently COMMITs and a later ROLLBACK undoes
    # nothing (failed bundles would leak rows). Take over transaction
    # control per the SQLAlchemy recipe: driver autocommit + explicit BEGIN.
    @event.listens_for(engine, "connect")
    def _sqlite_connect(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            # WAL lets readers run concurrently with the single writer —
            # required when several API processes share one database file.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _sqlite_begin(conn):
        conn.exec_driver_sql("BEGIN")


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, rollback on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    # Import models so they register on the metadata before create_all.
    from app import models

    assert models.Base is Base
    Base.metadata.create_all(bind=engine)
