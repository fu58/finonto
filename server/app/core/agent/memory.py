"""长期记忆：跨会话沉淀事实、模式与决策，并在推理时按需召回。

召回采用「向量相似 + 关键词命中 + 显著度」三路融合；被召回的记忆会提升显著度，
长期未使用的记忆按天衰减，避免上下文被陈旧结论占据。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import AgentRun, MemoryItem
from ...providers.base import ChatMessage
from ...providers.registry import registry
from ..text import candidate_terms, overlap, truncate

MEMORY_KINDS = ("fact", "pattern", "preference", "decision", "glossary")
VECTOR_NS = "memory:global"


@dataclass
class MemoryHit:
    id: int
    kind: str
    scope: str
    content: str
    salience: float
    score: float
    reason: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def remember(
        self,
        content: str,
        *,
        scope: str = "global",
        kind: str = "fact",
        keywords: list[str] | None = None,
        source_run_id: int | None = None,
        salience: float = 1.0,
        dedupe: bool = True,
    ) -> MemoryItem | None:
        content = (content or "").strip()
        if len(content) < 4:
            return None
        if dedupe:
            existing = self.session.scalars(
                select(MemoryItem).where(MemoryItem.scope == scope, MemoryItem.kind == kind)
            ).all()
            for item in existing:
                if overlap(content, item.content) > 0.85:
                    item.salience = min(2.0, item.salience + 0.15)
                    item.use_count += 1
                    item.last_used_at = _now()
                    self.session.flush()
                    return item
        embedder = registry.embedding()
        vector = embedder.embed([content])[0]
        item = MemoryItem(
            scope=scope,
            kind=kind if kind in MEMORY_KINDS else "fact",
            content=content,
            keywords=list(keywords or [t for t, _ in candidate_terms(content, max_terms=8)]),
            salience=float(salience),
            source_run_id=source_run_id,
            vector=np.asarray(vector, dtype=np.float32).tobytes(),
        )
        self.session.add(item)
        self.session.flush()
        registry.vector_store().upsert(VECTOR_NS, {item.id: np.asarray(vector, dtype=np.float32)}, {item.id: {"scope": scope, "kind": item.kind}})
        return item

    def recall(
        self,
        query: str,
        *,
        scope: str | None = None,
        kinds: list[str] | None = None,
        top_k: int = 5,
        min_salience: float = 0.2,
        reinforce: bool = True,
    ) -> list[MemoryHit]:
        items = list(
            self.session.scalars(
                select(MemoryItem).where(MemoryItem.salience >= min_salience)
                .order_by(MemoryItem.id.desc())
                .limit(4000)
            ).all()
        )
        if scope:
            items = [i for i in items if i.scope in (scope, "global")]
        if kinds:
            items = [i for i in items if i.kind in kinds]
        if not items:
            return []
        embedder = registry.embedding()
        query_vector = np.asarray(embedder.embed([query])[0], dtype=np.float32)
        scored: list[tuple[float, MemoryItem, str]] = []
        for item in items:
            dense = 0.0
            if item.vector:
                vec = np.frombuffer(item.vector, dtype=np.float32)
                if vec.shape == query_vector.shape:
                    dense = float(np.dot(vec, query_vector))
            lexical = overlap(query, item.content)
            keyword_hit = any(kw and kw in query for kw in (item.keywords or []))
            score = 0.45 * max(dense, 0.0) + 0.40 * lexical + (0.15 if keyword_hit else 0.0)
            score *= 1.0 + 0.08 * math.log1p(item.use_count)
            reason = f"向量 {dense:.2f} / 词面 {lexical:.2f}" + (" / 关键词命中" if keyword_hit else "")
            scored.append((score, item, reason))
        scored.sort(key=lambda row: -row[0])
        hits: list[MemoryHit] = []
        for score, item, reason in scored[:top_k]:
            if score < 0.08:
                continue
            if reinforce:
                item.use_count += 1
                item.last_used_at = _now()
                item.salience = min(2.0, item.salience + 0.05)
            hits.append(
                MemoryHit(
                    id=item.id,
                    kind=item.kind,
                    scope=item.scope,
                    content=item.content,
                    salience=round(item.salience, 3),
                    score=round(score, 4),
                    reason=reason,
                    created_at=item.created_at.isoformat() if item.created_at else "",
                )
            )
        if reinforce:
            self.session.flush()
        return hits

    def context_block(self, query: str, *, scope: str | None = "global", top_k: int = 4) -> str:
        hits = self.recall(query, scope=scope, top_k=top_k, reinforce=False)
        if not hits:
            return ""
        return "\n".join(f"- [{h.kind}] {truncate(h.content, 200)}" for h in hits)

    def list_items(self, scope: str | None = None, limit: int = 200) -> list[MemoryItem]:
        stmt = select(MemoryItem).order_by(MemoryItem.id.desc())
        if scope:
            stmt = stmt.where(MemoryItem.scope == scope)
        return list(self.session.scalars(stmt.limit(limit)).all())

    def forget(self, item_id: int) -> bool:
        item = self.session.get(MemoryItem, item_id)
        if item is None:
            return False
        registry.vector_store().delete(VECTOR_NS, [item_id])
        self.session.delete(item)
        self.session.flush()
        return True

    def decay(self, *, half_life_days: float = 45.0) -> int:
        now = _now()
        changed = 0
        for item in self.session.scalars(select(MemoryItem)).all():
            anchor = item.last_used_at or item.created_at
            if anchor is None:
                continue
            age_days = (now - anchor).total_seconds() / 86400
            target = max(0.15, math.exp(-math.log(2) * age_days / half_life_days))
            if item.salience > target:
                item.salience = round(target, 3)
                changed += 1
        self.session.flush()
        return changed

    async def consolidate_run(self, run_id: int, *, scope: str = "global") -> MemoryItem | None:
        """把一次完成的 Agent 运行压缩成可复用记忆。"""
        run = self.session.get(AgentRun, run_id)
        if run is None:
            return None
        from ...models import AgentStep

        steps = self.session.scalars(
            select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.ordinal)
        ).all()
        trace = "\n".join(
            f"{s.ordinal}. 行动 {s.action} -> {truncate(s.observation, 160)}" for s in steps[-8:]
        )
        prompt = (
            "[[TASK:memory_summary]]\n请把下面的 Agent 运行轨迹压缩成 1~2 句可长期复用的经验（保留具体对象名与结论）。\n\n"
            f"## 目标\n{run.goal}\n\n## 最终回答\n{run.final_answer}\n\n## 推理轨迹\n{trace}\n"
        )
        raw, _usage = await registry.llm().complete(
            [ChatMessage(role="user", content=prompt)], task="memory_summary", max_tokens=200
        )
        content = (raw or "").strip()
        if not content:
            content = f"任务「{truncate(run.goal, 60)}」已完成：{truncate(run.final_answer, 160)}"
        run.summary = content
        self.session.flush()
        return self.remember(
            content,
            scope=scope,
            kind="pattern",
            source_run_id=run_id,
            keywords=[t for t, _ in candidate_terms(run.goal + " " + content, max_terms=10)],
            salience=1.1,
        )

    def stats(self) -> dict[str, Any]:
        by_kind = dict(
            self.session.execute(select(MemoryItem.kind, func.count()).group_by(MemoryItem.kind)).all()
        )
        return {"total": int(self.session.scalar(select(func.count()).select_from(MemoryItem)) or 0), "by_kind": by_kind}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def prune(session: Session, *, older_than_days: int = 180, min_salience: float = 0.2) -> int:
    cutoff = _now() - timedelta(days=older_than_days)
    rows = session.scalars(
        select(MemoryItem).where(
            MemoryItem.salience < min_salience,
            MemoryItem.created_at < cutoff,
        )
    ).all()
    for row in rows:
        registry.vector_store().delete(VECTOR_NS, [row.id])
        session.delete(row)
    session.flush()
    return len(rows)
