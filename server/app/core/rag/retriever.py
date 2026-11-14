"""混合检索：BM25 + 向量 → RRF 融合 → 去重配额 → 图感知扩展 → MMR 多样化。

每一级都能解释「为什么这条被召回」：``RetrievedChunk.signals`` 记录了它来自哪一路、
融合后的排名、是否被图感知加权。这份信号会写进问答轨迹，供复盘与调参。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from sqlalchemy import select

from ...models import Document, DocumentChunk
from ..text import term_set
from .bm25 import BM25Index
from .query_understanding import QueryPlan

RRF_K = 60


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: int
    document_id: int
    doc_title: str
    locator: str
    text: str
    score: float = 0.0
    signals: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "doc_title": self.doc_title,
            "locator": self.locator,
            "text": self.text,
            "score": round(self.score, 6),
            "signals": self.signals,
        }


# --------------------------------------------------------------------------- #
# BM25 索引缓存（按空间 + 片段数量签名失效）
# --------------------------------------------------------------------------- #
_bm25_cache: dict[int, tuple[tuple[int, int], BM25Index]] = {}
_bm25_lock = threading.Lock()


@dataclass(slots=True)
class ChunkRow:
    chunk_id: int
    document_id: int
    ordinal: int
    text: str
    locator: str
    doc_title: str
    doc_type: str
    jurisdiction: str
    authority: int


def load_chunks(session, space_id: int) -> dict[int, ChunkRow]:
    rows = session.execute(
        select(
            DocumentChunk.id,
            DocumentChunk.document_id,
            DocumentChunk.ordinal,
            DocumentChunk.text,
            DocumentChunk.locator,
            Document.title,
            Document.doc_type,
            Document.jurisdiction,
            Document.authority,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.space_id == space_id)
    ).all()
    return {
        int(chunk_id): ChunkRow(
            chunk_id=int(chunk_id),
            document_id=int(document_id),
            ordinal=int(ordinal),
            text=text,
            locator=locator,
            doc_title=title,
            doc_type=doc_type or "",
            jurisdiction=jurisdiction or "",
            authority=int(authority or 3),
        )
        for chunk_id, document_id, ordinal, text, locator, title, doc_type, jurisdiction, authority in rows
    }


def get_bm25(session, space_id: int, rows: dict[int, ChunkRow]) -> BM25Index:
    signature = (len(rows), max(rows) if rows else 0)
    cached = _bm25_cache.get(space_id)
    if cached and cached[0] == signature:
        return cached[1]
    with _bm25_lock:
        cached = _bm25_cache.get(space_id)
        if cached and cached[0] == signature:
            return cached[1]
        index = BM25Index().build([(chunk_id, row.text) for chunk_id, row in rows.items()])
        _bm25_cache[space_id] = (signature, index)
        return index


def invalidate_cache(space_id: int | None = None) -> None:
    with _bm25_lock:
        if space_id is None:
            _bm25_cache.clear()
        else:
            _bm25_cache.pop(space_id, None)


# --------------------------------------------------------------------------- #
# 结构化过滤
# --------------------------------------------------------------------------- #
def _passes_filters(row: ChunkRow, filters: dict) -> bool:
    doc_types = filters.get("doc_types") or []
    if doc_types and row.doc_type not in doc_types:
        return False
    min_authority = filters.get("min_authority")
    if min_authority and row.authority < int(min_authority):
        return False
    jurisdiction = filters.get("jurisdiction")
    if jurisdiction and row.jurisdiction and row.jurisdiction != jurisdiction:
        return False
    return True


# --------------------------------------------------------------------------- #
# RRF 融合
# --------------------------------------------------------------------------- #
def rrf_fuse(rankings: dict[str, list[int]], weights: dict[str, float] | None = None) -> dict[int, dict]:
    weights = weights or {}
    fused: dict[int, dict] = {}
    for source, ordered in rankings.items():
        weight = weights.get(source, 1.0)
        for rank, chunk_id in enumerate(ordered, start=1):
            entry = fused.setdefault(chunk_id, {"score": 0.0, "sources": {}})
            entry["score"] += weight / (RRF_K + rank)
            entry["sources"][source] = rank
    return fused


def mmr_select(
    candidates: list[RetrievedChunk],
    *,
    top_k: int,
    lambda_param: float = 0.72,
) -> list[RetrievedChunk]:
    """最大边际相关：在「相关」与「新颖」之间折中，避免返回 5 条几乎一样的段落。"""
    if len(candidates) <= top_k:
        return candidates
    token_sets = {item.chunk_id: term_set(item.text) for item in candidates}
    selected: list[RetrievedChunk] = []
    pool = sorted(candidates, key=lambda item: item.score, reverse=True)
    while pool and len(selected) < top_k:
        best_index, best_value = 0, float("-inf")
        for index, item in enumerate(pool):
            redundancy = 0.0
            if selected:
                item_terms = token_sets[item.chunk_id]
                redundancy = max(
                    len(item_terms & token_sets[chosen.chunk_id])
                    / max(1, len(item_terms | token_sets[chosen.chunk_id]))
                    for chosen in selected
                )
            value = lambda_param * item.score - (1 - lambda_param) * redundancy
            if value > best_value:
                best_value, best_index = value, index
        selected.append(pool.pop(best_index))
    return selected


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def retrieve(
    session,
    space_id: int,
    plan: QueryPlan,
    *,
    components,
    top_k: int = 8,
    per_doc_quota: int = 3,
) -> list[RetrievedChunk]:
    rows = load_chunks(session, space_id)
    if not rows:
        return []

    query_text = plan.question
    if plan.expanded_terms:
        query_text += " " + " ".join(plan.expanded_terms)

    # ---- 路 1：BM25 ----
    bm25 = get_bm25(session, space_id, rows)
    bm25_hits = [hit for hit in bm25.search(query_text, top_k=40) if hit.doc_id in rows]
    bm25_hits = [hit for hit in bm25_hits if _passes_filters(rows[hit.doc_id], plan.filters)]

    # ---- 路 2：向量 ----
    vector_ids: list[int] = []
    try:
        vector = components.embedding.embed_one(query_text)
        allowed = {cid for cid, row in rows.items() if _passes_filters(row, plan.filters)}
        vector_hits = components.vector_store.search(vector, top_k=40, allowed_ids=allowed)
        vector_ids = [hit.chunk_id for hit in vector_hits if hit.chunk_id in rows]
    except Exception:  # noqa: BLE001 - 向量路失败时退化为纯 BM25
        vector_ids = []

    rankings: dict[str, list[int]] = {"bm25": [hit.doc_id for hit in bm25_hits]}
    weights = {"bm25": 1.0, "vector": 1.0}
    if vector_ids:
        rankings["vector"] = vector_ids
    else:
        weights["bm25"] = 1.6  # 只有一路时提升其权重，避免融合后分数过小

    fused = rrf_fuse(rankings, weights)

    # ---- 图感知：命中已有类型则加权 ----
    aligned_labels = [item["label"] for item in plan.aligned_types if item.get("label")]
    boosts: dict[int, float] = {}
    for chunk_id in fused:
        text = rows[chunk_id].text
        for label in aligned_labels:
            if label and label in text:
                boosts[chunk_id] = boosts.get(chunk_id, 0.0) + 0.02
                break

    ordered = sorted(
        fused.items(),
        key=lambda item: item[1]["score"] + boosts.get(item[0], 0.0),
        reverse=True,
    )

    # ---- 同文档配额 ----
    quota: dict[int, int] = {}
    picked: list[RetrievedChunk] = []
    overflow: list[RetrievedChunk] = []
    for chunk_id, info in ordered:
        row = rows[chunk_id]
        item = RetrievedChunk(
            chunk_id=chunk_id,
            document_id=row.document_id,
            doc_title=row.doc_title,
            locator=row.locator,
            text=row.text,
            score=info["score"] + boosts.get(chunk_id, 0.0),
            signals={
                "sources": info["sources"],
                "rrf": round(info["score"], 6),
                "graph_boost": round(boosts.get(chunk_id, 0.0), 4),
                "authority": row.authority,
                "doc_type": row.doc_type,
            },
        )
        if quota.get(row.document_id, 0) < per_doc_quota:
            quota[row.document_id] = quota.get(row.document_id, 0) + 1
            picked.append(item)
        else:
            overflow.append(item)

    candidates = picked + overflow

    # ---- MMR 多样化 ----
    selected = mmr_select(candidates, top_k=max(top_k, 1))
    for rank, item in enumerate(selected, start=1):
        item.signals["final_rank"] = rank
        item.signals["aligned_types"] = aligned_labels
    return selected
