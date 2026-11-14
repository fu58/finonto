"""数据库引擎与事务边界。

SQLite 关键设置
--------------
* ``journal_mode=WAL``      —— 读写不互斥，读请求不会被正在提交的写事务卡住。
* ``synchronous=NORMAL``    —— WAL 下的推荐值，牺牲极小安全性换明显吞吐。
* ``foreign_keys=ON``       —— SQLite 默认关闭外键，必须每连接打开，否则 CASCADE 不生效。
* ``busy_timeout=5000``     —— 并发写入时排队而不是立刻 ``database is locked``。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _apply_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA temp_store=MEMORY")
    finally:
        cursor.close()


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        settings.ensure_dirs()
        url = settings.resolved_database_url()
        is_sqlite = url.startswith("sqlite")
        _engine = create_engine(
            url,
            future=True,
            echo=False,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False} if is_sqlite else {},
        )
        if is_sqlite:
            event.listen(_engine, "connect", _apply_pragmas)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """一个业务操作一个事务：正常提交，异常回滚。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖注入用。"""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """建表（幂等）。生产环境改表请引入 Alembic，本项目冷启动足够。"""
    import app.models.domain  # noqa: F401  确保所有模型已注册到 metadata

    Base.metadata.create_all(bind=get_engine())


def reset_engine() -> None:
    """测试用：切换数据库后重新建引擎。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
