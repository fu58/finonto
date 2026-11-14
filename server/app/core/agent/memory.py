"""长期记忆：条目化存储 + 关键词召回 + 时间衰减 + 归纳与清理。

为什么不做向量记忆
-----------------
记忆条目通常只有几十到几百条，关键词召回足够；向量化会让「为什么想起了这条」
变得不可解释，而 Agent 的错误往往就出在「想起了不该想的」。

时间衰减：``weight`` 每 30 天乘 0.9，低权重条目在 ``prune`` 时优先清理。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from ...models import Memory, utcnow
from ..text import normalize, term_set, truncate


def save(session, space_id: int, content: str, *, kind: str = "fact", keywords: str = "") -> Memory:
    content = normalize(content)[:2000]
    if not keywords:
        tokens = list(term_set(content))[:8]
        keywords = ",".join(tokens)
    item = Memory(
        space_id=space_id,
        kind=kind,
        content=content,
        keywords=keywords[:300],
        weight=1.0,
        hits=0,
    )
    session.add(item)
    session.flush()
    return item


def search(session, space_id: int, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Memory).where(Memory.space_id == space_id).order_by(Memory.weight.desc()).limit(200)
    ).scalars().all()
    query_terms = term_set(query)
    scored: list[tuple[float, Memory]] = []
    for item in rows:
        item_terms = term_set(item.content) | set((item.keywords or "").split(","))
        overlap = len(query_terms & item_terms)
        score = overlap * 0.6 + item.weight * 0.4
        if overlap or not query_terms:
            scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    picked = scored[:limit]
    for _score, item in picked:
        item.hits += 1
        item.last_used_at = utcnow()
    session.flush()
    return [
        {
            "id": item.id,
            "kind": item.kind,
            "content": truncate(item.content, 300),
            "keywords": item.keywords,
            "weight": round(item.weight, 2),
            "hits": item.hits,
        }
        for _score, item in picked
    ]


def consolidate(session, space_id: int, run_summary: str, *, keywords: str = "") -> Memory | None:
    """运行结束后自动归纳一条记忆（去重：同空间同内容不重复写）。"""
    summary = truncate(normalize(run_summary), 400)
    if len(summary) < 12:
        return None
    existing = session.execute(
        select(Memory).where(Memory.space_id == space_id, Memory.content == summary)
    ).scalars().first()
    if existing is not None:
        existing.weight = min(3.0, existing.weight + 0.2)
        session.flush()
        return existing
    return save(session, space_id, summary, kind="insight", keywords=keywords)


def decay(session, space_id: int, *, half_life_days: int = 30) -> int:
    """按时间衰减权重（每次调用只衰减一次，幂等性由调用方保证）。"""
    rows = session.execute(select(Memory).where(Memory.space_id == space_id)).scalars().all()
    now = utcnow()
    changed = 0
    for item in rows:
        age_days = max(0.0, (now - item.created_at).total_seconds() / 86400)
        factor = math.pow(0.5, age_days / max(1, half_life_days))
        new_weight = max(0.05, min(3.0, round(factor, 3)))
        if abs(new_weight - item.weight) > 1e-6:
            item.weight = new_weight
            changed += 1
    session.flush()
    return changed


def prune(session, space_id: int, *, min_weight: float = 0.2, keep_hits: int = 3) -> int:
    rows = session.execute(select(Memory).where(Memory.space_id == space_id)).scalars().all()
    removed = 0
    for item in rows:
        if item.weight < min_weight and item.hits < keep_hits:
            session.delete(item)
            removed += 1
    session.flush()
    return removed


def list_memories(session, space_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Memory).where(Memory.space_id == space_id).order_by(Memory.created_at.desc()).limit(limit)
    ).scalars().all()
    return [
        {
            "id": item.id,
            "kind": item.kind,
            "content": item.content,
            "keywords": item.keywords,
            "weight": round(item.weight, 2),
            "hits": item.hits,
            "created_at": item.created_at.isoformat(sep=" ", timespec="seconds"),
        }
        for item in rows
    ]


def stats(session, space_id: int) -> dict[str, Any]:
    total = int(
        session.execute(
            select(func.count(Memory.id)).where(Memory.space_id == space_id)
        ).scalar_one()
    )
    avg = session.execute(
        select(func.avg(Memory.weight)).where(Memory.space_id == space_id)
    ).scalar_one()
    return {"count": total, "avg_weight": round(float(avg or 0), 3)}
