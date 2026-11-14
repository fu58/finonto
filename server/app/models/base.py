"""SQLAlchemy 声明式基类与通用列。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    """统一使用「无时区 UTC」存库：SQLite 友好，序列化无需再处理偏移。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass
