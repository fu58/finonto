"""混合检索：BM25 稀疏通道 + 向量稠密通道 + RRF 融合 + 图感知扩展。

针对中文制度/标准语料做了三件专门处理：
1) 文档级配额，避免单一长文档刷屏；
2) 相邻块补全，条款被切断时把上一/下一块一起带回；
3) 本体感知加权，命中已有对象类型名称的块获得提升。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import Chunk, ObjectType
from ...providers.base import ProviderError
from ...providers.registry import registry
from ..text import jaccard, tokenize, truncate
from .bm25 import index_registry
from .ingest import namespace_for
from .query_understanding import UnderstoodQuery


@dataclass
class Candidate:
    chunk_id: int
    doc_id: int
    text: str
    locator: str
    meta: dict[str, Any] = field(default_factory=dict)
    lexical_rank: int | None = None
    dense_rank: int | None = None
    lexical_score: float = 0.0
    dense_score: float = 0.0
    fused_score: float = 0.0
    final_score: float = 0.0
    rank: int = 0
    reasons: list[str] = field(default_factory=list)
    ref: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "doc_name": self.meta.get("doc_name", ""),
            "doc_type": self.meta.get("doc_type", ""),
            "locator": self.locator,
            "ordinal": self.meta.get("ordinal"),
            "lexical_rank": self.lexical_rank,
            "dense_rank": self.dense_rank,
            "lexical_score": round(self.lexical_score, 4),
            "dense_score": round(self.dense_score, 4),
            "fused_score": round(self.fused_score, 5),
            "final_score": round(self.final_score, 4),
            "ref": self.ref,
            "reasons": self.reasons,
            "preview": truncate(self.text, 120),
        }


class HybridRetriever:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------ 通道
    def lexical(
        self, understood: UnderstoodQuery, space_id: int, top_k: int
    ) -> dict[int, tuple[int, float]]:
        ns = namespace_for(space_id)
        index = index_registry.get(ns)
        if not index.size:
            self._rebuild(space_id)
            index = index_registry.get(ns)
        query_text = understood.search_text
        hits = index.search(query_text, top_k)
        if not hits and understood.expansions:
            hits = index.search(understood.rewritten or understood.original, top_k)
        return {hit.ref_id: (rank + 1, hit.score) for rank, hit in enumerate(hits)}

    def dense(
        self, understood: UnderstoodQuery, space_id: int, top_k: int
    ) -> dict[int, tuple[int, float]]:
        try:
            embedder = registry.embedding()
            store = registry.vector_store()
        except ProviderError as exc:
            understood.notes.append(f"稠密通道不可用: {exc}")
            return {}
        ns = namespace_for(space_id)
        if store.count(ns) == 0:
            return {}
        variants = [understood.rewritten or understood.original, *understood.expansions[:4], *understood.sub_questions[:3]]
        variants = [v for v in dict.fromkeys(variants) if v]
        if not variants:
            return {}
        matrix = embedder.embed(variants)
        best: dict[int, float] = {}
        for row in matrix:
            for hit in store.search(ns, np.asarray(row, dtype=np.float32), top_k):
                prev = best.get(hit.ref_id, -1.0)
                best[hit.ref_id] = max(prev, hit.score)
        ordered = sorted(best.items(), key=lambda kv: -kv[1])
        return {ref_id: (rank + 1, score) for rank, (ref_id, score) in enumerate(ordered)}

    # ------------------------------------------------------------ 融合
    def search(
        self,
        understood: UnderstoodQuery,
        space_id: int,
        *,
        candidate_k: int | None = None,
        final_k: int | None = None,
        graph_expand: bool = True,
    ) -> list[Candidate]:
        candidate_k = candidate_k or settings.rag_candidate_k
        final_k = final_k or settings.rag_final_k
        lexical = self.lexical(understood, space_id, settings.rag_top_k_bm25)
        dense = self.dense(understood, space_id, settings.rag_top_k_dense)
        if not lexical and not dense:
            return []

        fused: dict[int, float] = {}
        if settings.rag_fusion == "rrf":
            k = 60
            for ref_id, (rank, _score) in lexical.items():
                fused[ref_id] = fused.get(ref_id, 0.0) + 1.0 / (k + rank)
            for ref_id, (rank, score) in dense.items():
                bonus = 1.0 if score > 0 else 0.35
                fused[ref_id] = fused.get(ref_id, 0.0) + bonus / (k + rank)
        else:
            lex_max = max((s for _r, (_rk, s) in lexical.items()), default=1.0) or 1.0
            for ref_id, (_rank, score) in lexical.items():
                fused[ref_id] = fused.get(ref_id, 0.0) + 0.6 * score / lex_max
            for ref_id, (_rank, score) in dense.items():
                fused[ref_id] = fused.get(ref_id, 0.0) + 0.4 * max(score, 0.0)

        rows = self.session.scalars(select(Chunk).where(Chunk.id.in_(list(fused)))).all()
        candidates: list[Candidate] = []
        for chunk in rows:
            meta = dict(chunk.meta or {})
            lr = lexical.get(chunk.id)
            dr = dense.get(chunk.id)
            candidate = Candidate(
                chunk_id=chunk.id,
                doc_id=chunk.doc_id,
                text=chunk.text,
                locator=chunk.locator,
                meta={**meta, "ordinal": chunk.ordinal},
                lexical_rank=lr[0] if lr else None,
                dense_rank=dr[0] if dr else None,
                lexical_score=lr[1] if lr else 0.0,
                dense_score=dr[1] if dr else 0.0,
                fused_score=fused.get(chunk.id, 0.0),
            )
            if not self._passes_filters(candidate, understood.filters):
                continue
            candidates.append(candidate)

        candidates.sort(key=lambda c: -c.fused_score)
        candidates = self._collapse_duplicates(candidates)
        candidates = self._apply_doc_quota(candidates, per_doc=max(3, final_k // 2))
        if graph_expand:
            candidates = self._graph_aware_expand(candidates, understood, space_id)
        candidates = self._mmr(candidates, final_k, lam=settings.rag_mmr_lambda)
        for i, candidate in enumerate(candidates):
            candidate.rank = i + 1
            candidate.ref = f"c{i + 1}"
        return candidates

    # ------------------------------------------------------------ 过滤
    def _passes_filters(self, candidate: Candidate, filters: dict[str, Any]) -> bool:
        doc_type = filters.get("doc_type")
        if doc_type and candidate.meta.get("doc_type") and candidate.meta["doc_type"] != doc_type:
            # 只做偏好而非硬过滤：完全排除会让空结果频发
            candidate.reasons.append(f"doc_type≠{doc_type}（降权）")
            candidate.fused_score *= 0.75
        min_authority = filters.get("min_authority")
        if min_authority and float(candidate.meta.get("authority", 0.5)) < float(min_authority):
            candidate.fused_score *= 0.7
            candidate.reasons.append("authority 低于期望值（降权）")
        return True

    # ------------------------------------------------------------ 处理
    def _collapse_duplicates(self, candidates: list[Candidate]) -> list[Candidate]:
        seen: set[str] = set()
        out: list[Candidate] = []
        for candidate in candidates:
            key = candidate.text[:60].replace(" ", "")
            if key in seen:
                if out:
                    out[-1].reasons.append("重复内容已合并")
                continue
            seen.add(key)
            out.append(candidate)
        return out

    def _apply_doc_quota(self, candidates: list[Candidate], *, per_doc: int) -> list[Candidate]:
        counts: dict[int, int] = {}
        kept: list[Candidate] = []
        overflow: list[Candidate] = []
        for candidate in candidates:
            used = counts.get(candidate.doc_id, 0)
            if used >= per_doc:
                candidate.reasons.append("同文档配额限制，降级")
                overflow.append(candidate)
                continue
            counts[candidate.doc_id] = used + 1
            kept.append(candidate)
        return kept + overflow

    def _graph_aware_expand(
        self, candidates: list[Candidate], understood: UnderstoodQuery, space_id: int
    ) -> list[Candidate]:
        """两类扩展：相邻块补全（条款被切断）+ 本体名称命中的加权。"""
        labels = [t.label for t in self.session.scalars(select(ObjectType).where(ObjectType.space_id == space_id))]
        label_set = {label for label in labels if len(label) >= 3}
        present = {c.chunk_id: c for c in candidates}
        if label_set:
            for candidate in candidates:
                hits = [label for label in label_set if label in candidate.text]
                if hits:
                    candidate.fused_score *= 1.0 + min(0.06 * len(hits), 0.25)
                    candidate.reasons.append("命中已有对象类型: " + "、".join(hits[:3]))
        neighbour_ids: dict[int, list[tuple[int, int]]] = {}
        for candidate in candidates[: max(3, len(candidates) // 2)]:
            ordinal = int(candidate.meta.get("ordinal", 0) or 0)
            neighbour_ids[candidate.doc_id] = neighbour_ids.get(candidate.doc_id, [])
            neighbour_ids[candidate.doc_id].append((ordinal, candidate.rank or 0))
        wanted: list[tuple[int, int]] = []
        for doc_id, pairs in neighbour_ids.items():
            for ordinal, _rank in pairs:
                wanted.extend((doc_id, ordinal + delta) for delta in (-1, 1))
        if not wanted:
            return candidates
        conditions = or_(*[
            (Chunk.doc_id == doc_id) & (Chunk.ordinal == ordinal) for doc_id, ordinal in set(wanted)
        ])
        extras = self.session.scalars(
            select(Chunk).where(conditions)
        ).all()
        boost = min(candidate.fused_score for candidate in candidates) * 0.35 if candidates else 1e-6
        for chunk in extras:
            if chunk.id in present:
                continue
            candidate = Candidate(
                chunk_id=chunk.id,
                doc_id=chunk.doc_id,
                text=chunk.text,
                locator=chunk.locator,
                meta={**(chunk.meta or {}), "ordinal": chunk.ordinal},
                fused_score=boost,
                reasons=["相邻块补全（条款连续性）"],
            )
            present[chunk.id] = candidate
            candidates.append(candidate)
        candidates.sort(key=lambda c: -c.fused_score)
        return candidates

    def _mmr(self, candidates: list[Candidate], k: int, *, lam: float = 0.35) -> list[Candidate]:
        if not candidates:
            return []
        token_sets = [set(tokenize(c.text)) for c in candidates]
        top_relevance = max(candidate.fused_score for candidate in candidates) or 1.0
        selected: list[int] = []
        pool = list(range(len(candidates)))
        while pool and len(selected) < k:
            best_idx, best_value = pool[0], -1e9
            for i in pool:
                relevance = candidates[i].fused_score
                diversity = 0.0
                for j in selected:
                    diversity = max(diversity, jaccard(token_sets[i], token_sets[j]))
                value = (1 - lam) * relevance - lam * diversity * top_relevance
                if value > best_value:
                    best_idx, best_value = i, value
            selected.append(best_idx)
            pool.remove(best_idx)
        return [candidates[i] for i in selected]

    def _rebuild(self, space_id: int) -> int:
        from .ingest import IngestService

        return IngestService(self.session).rebuild_lexical_index(space_id)

    def read_chunks(self, chunk_ids: list[int]) -> list[Chunk]:
        if not chunk_ids:
            return []
        return list(self.session.scalars(select(Chunk).where(Chunk.id.in_(chunk_ids))).all())
