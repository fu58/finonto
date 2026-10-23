"""SQLAlchemy 引擎与会话（默认 SQLite，可切 Postgres）。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


_engine_kwargs: dict = {"future": True, "pool_pre_ping": True}
if settings.resolved_database_url.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}

engine = create_engine(settings.resolved_database_url, **_engine_kwargs)


if settings.resolved_database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI 依赖。"""
    with session_scope() as session:
        yield session


def init_db() -> None:
    from . import models  # noqa: F401  确保映射注册

    Base.metadata.create_all(bind=engine)
