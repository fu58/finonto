"""RAG 主链路编排：理解 -> 混合检索 -> 重排序 -> 流式生成 -> 可观测落库。"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import ObjectType, QueryTrace
from ...providers.base import WebResult
from ...providers.registry import registry
from ..ontology.packs import lexicon_for
from .generator import stream_answer
from .query_understanding import UnderstoodQuery, understand_async
from .reranker import rerank
from .retriever import Candidate, HybridRetriever


class RagPipeline:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.retriever = HybridRetriever(session)

    # ------------------------------------------------------------ 组件
    def known_types(self, space_id: int) -> list[tuple[int, str, str]]:
        rows = self.session.scalars(
            select(ObjectType).where(ObjectType.space_id == space_id)
        ).all()
        return [(r.id, r.label, r.definition or "") for r in rows]

    async def prepare(
        self,
        question: str,
        space_id: int,
        *,
        history: list[dict[str, str]] | None = None,
        final_k: int | None = None,
        rerank_provider: str | None = None,
    ) -> tuple[UnderstoodQuery, list[Candidate], str, list[WebResult]]:
        lexicon = lexicon_for(self.session, space_id)
        understood = await understand_async(
            question,
            history=history,
            known_types=self.known_types(space_id),
            synonyms=lexicon["synonyms"],
            extra_suffixes=lexicon["term_suffixes"],
        )
        candidates = self.retriever.search(
            understood, space_id, candidate_k=settings.rag_candidate_k * 2, final_k=settings.rag_candidate_k
        )
        web_results: list[WebResult] = []
        if understood.needs_web or not candidates:
            web_results = _web_search(understood, self.session)
            candidates = _merge_web(candidates, web_results, final_k or settings.rag_candidate_k)
        ranked, strategy = await rerank(understood, candidates, final_k or settings.rag_final_k, provider=rerank_provider)
        final: list[Candidate] = []
        for candidate, score, reason in ranked:
            candidate.final_score = score
            candidate.reasons.append(f"重排 {strategy}: {reason}")
            final.append(candidate)
        for i, candidate in enumerate(final, start=1):
            candidate.rank = i
            candidate.ref = f"c{i}"
        return understood, final, strategy, web_results

    async def run(
        self,
        question: str,
        space_id: int,
        *,
        history: list[dict[str, str]] | None = None,
        final_k: int | None = None,
        rerank_provider: str | None = None,
        extra: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        started = time.perf_counter()
        answer_id = uuid.uuid4().hex[:16]
        try:
            understood, candidates, strategy, web_results = await self.prepare(
                question,
                space_id,
                history=history,
                final_k=final_k,
                rerank_provider=rerank_provider,
            )
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "stage": "prepare", "message": str(exc)[:400]}
            return

        known_labels = [label for _id, label, _d in self.known_types(space_id)]
        domain_label = lexicon_for(self.session, space_id)["label"]
        yield {
            "type": "stage",
            "stage": "understood",
            "answer_id": answer_id,
            "understood": understood.to_dict(),
        }
        yield {
            "type": "stage",
            "stage": "retrieved",
            "answer_id": answer_id,
            "strategy": strategy,
            "candidates": [c.as_dict() for c in candidates],
            "web_results": [w.__dict__ for w in web_results],
            "empty": not candidates,
        }

        answer_parts: list[str] = []
        citations: list[dict[str, Any]] = []
        coverage: dict[str, Any] = {}
        async for event in stream_answer(
            understood,
            candidates,
            session=self.session,
            known_types=known_labels,
            extra=extra,
            answer_id=answer_id,
            domain_label=domain_label,
        ):
            if event["type"] == "context":
                continue
            if event["type"] == "token":
                answer_parts.append(event["text"])
            elif event["type"] == "citations":
                citations = event["citations"]
            elif event["type"] == "done":
                coverage = event["coverage"]
            yield event

        self._persist(
            answer_id=answer_id,
            space_id=space_id,
            question=question,
            understood=understood,
            candidates=candidates,
            strategy=strategy,
            answer="".join(answer_parts),
            citations=citations,
            coverage=coverage,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        # 流式响应期间事务保持打开会阻塞其它写入，落库后立即释放
        self.session.commit()

    def _persist(
        self,
        *,
        answer_id: str,
        space_id: int,
        question: str,
        understood: UnderstoodQuery,
        candidates: list[Candidate],
        strategy: str,
        answer: str,
        citations: list[dict],
        coverage: dict,
        elapsed_ms: int,
    ) -> QueryTrace:
        trace = QueryTrace(
            answer_id=answer_id,
            space_id=space_id,
            question=question,
            understood=understood.to_dict(),
            retrieval=[c.as_dict() for c in candidates],
            rerank=[
                {"ref": c.ref, "chunk_id": c.chunk_id, "score": c.final_score, "strategy": strategy, "reasons": c.reasons}
                for c in candidates
            ],
            answer=answer,
            citations=citations,
            coverage=float((coverage or {}).get("coverage", 0.0)),
            elapsed_ms=elapsed_ms,
        )
        self.session.add(trace)
        self.session.flush()
        return trace

    def recent_traces(self, space_id: int | None, limit: int = 20) -> list[QueryTrace]:
        stmt = select(QueryTrace).order_by(QueryTrace.id.desc())
        if space_id:
            stmt = stmt.where(QueryTrace.space_id == space_id)
        return list(self.session.scalars(stmt.limit(limit)).all())


def _web_search(understood: UnderstoodQuery, session: Session) -> list[WebResult]:
    provider = registry.web_search()
    if provider.name == "none":
        understood.notes.append("网络搜索组件未启用，仅使用本地知识库")
        return []
    try:
        results = provider.search(understood.rewritten or understood.original, top_k=4)
    except Exception as exc:  # noqa: BLE001
        understood.notes.append(f"网络搜索失败: {str(exc)[:120]}")
        return []
    understood.notes.append(f"联网补充 {len(results)} 条结果")
    return results


def _merge_web(candidates: list[Candidate], results: list[WebResult], limit: int) -> list[Candidate]:
    if not results:
        return candidates
    extra: list[Candidate] = []
    for i, result in enumerate(results):
        text = f"{result.title}\n{result.snippet}"
        if not text.strip():
            continue
        extra.append(
            Candidate(
                chunk_id=-(i + 1),
                doc_id=0,
                text=text,
                locator=result.url,
                meta={
                    "doc_name": result.title or result.url,
                    "doc_type": f"网络检索({result.source})",
                    "authority": 0.35,
                    "url": result.url,
                },
                fused_score=0.02 + 0.01 * (len(results) - i),
                reasons=["联网补充来源"],
            )
        )
    return (candidates + extra)[: max(limit, len(candidates))]
