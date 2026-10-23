"""RAG 问答接口：流式生成（带引用）、单次检索、查询理解预览、历史轨迹。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..core.ontology.packs import lexicon_for
from ..core.rag.pipeline import RagPipeline
from ..core.rag.query_understanding import understand
from ..db import get_session
from .sse import sse_response

router = APIRouter(prefix="/api/rag", tags=["rag"])


@router.get("/stream")
async def stream_answer(
    question: str,
    space_id: int = 1,
    final_k: int | None = None,
    rerank_provider: str | None = None,
    history: str = "",
    session: Session = Depends(get_session),
):
    """SSE 流：stage → token → citations → done。"""
    if not question.strip():
        raise HTTPException(400, "question 不能为空")
    parsed_history = _parse_history(history)
    return sse_response(
        RagPipeline(session).run(
            question,
            space_id,
            history=parsed_history,
            final_k=final_k,
            rerank_provider=rerank_provider,
        )
    )


@router.post("/ask")
async def ask(body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    """非流式问答：一次拿到完整回答、引用与检索轨迹（便于脚本与测试）。"""
    question = str(body.get("question", "")).strip()
    if not question:
        raise HTTPException(400, "question 不能为空")
    space_id = int(body.get("space_id", 1))
    answer_parts: list[str] = []
    payload: dict[str, Any] = {}
    citations: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    understood: dict[str, Any] = {}
    async for event in RagPipeline(session).run(
        question,
        space_id,
        history=_parse_history(str(body.get("history", ""))),
        final_k=body.get("final_k"),
        rerank_provider=body.get("rerank_provider"),
    ):
        if event["type"] == "token":
            answer_parts.append(event["text"])
        elif event["type"] == "citations":
            citations = event["citations"]
        elif event["type"] == "done":
            payload = event
        elif event["type"] == "error":
            raise HTTPException(502, event["message"])
        elif event.get("stage") == "understood":
            understood = event["understood"]
        elif event.get("stage") == "retrieved":
            candidates = event["candidates"]
    # done 事件里已带 answer，以流式拼装结果为准，保证与前端逐字渲染一致
    return {**payload, "answer": "".join(answer_parts) or payload.get("answer", ""),
            "citations": citations, "candidates": candidates, "understood": understood}


@router.post("/search")
async def search_only(body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    """只跑查询理解 → 混合检索 → 重排，不生成答案：便于观察召回质量。"""
    question = str(body.get("question", "")).strip()
    if not question:
        raise HTTPException(400, "question 不能为空")
    space_id = int(body.get("space_id", 1))
    understood, candidates, strategy, web_results = await RagPipeline(session).prepare(
        question, space_id, final_k=body.get("final_k"), rerank_provider=body.get("rerank_provider")
    )
    return {
        "understood": understood.to_dict(),
        "strategy": strategy,
        "candidates": [c.as_dict() for c in candidates],
        "web": [vars(r) for r in web_results],
        "notes": understood.notes,
    }


@router.get("/understand")
def understand_query(question: str, space_id: int = 1, session: Session = Depends(get_session)) -> dict[str, Any]:
    """查询理解的规则部分（意图、同义扩展、子问题、过滤条件、已有类型对齐）。"""
    pipeline = RagPipeline(session)
    lexicon = lexicon_for(session, space_id)
    understood = understand(
        question,
        known_types=pipeline.known_types(space_id),
        synonyms=lexicon["synonyms"],
        extra_suffixes=lexicon["term_suffixes"],
    )
    return understood.to_dict()


@router.get("/traces")
def recent_traces(space_id: int | None = None, limit: int = 20, session: Session = Depends(get_session)) -> dict[str, Any]:
    traces = RagPipeline(session).recent_traces(space_id, limit=limit)
    return {
        "traces": [
            {
                "id": t.id,
                "answer_id": t.answer_id,
                "question": t.question,
                "answer": t.answer,
                "coverage": t.coverage,
                "elapsed_ms": t.elapsed_ms,
                "citation_count": len(t.citations or []),
                "created_at": t.created_at.isoformat() if t.created_at else "",
            }
            for t in traces
        ]
    }


@router.get("/trace/{answer_id}")
def trace_detail(answer_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    from sqlalchemy import select

    from ..models import QueryTrace

    trace = session.scalar(select(QueryTrace).where(QueryTrace.answer_id == answer_id))
    if trace is None:
        raise HTTPException(404, "轨迹不存在")
    return {
        "id": trace.id,
        "answer_id": trace.answer_id,
        "question": trace.question,
        "understood": trace.understood,
        "retrieval": trace.retrieval,
        "rerank": trace.rerank,
        "answer": trace.answer,
        "citations": trace.citations,
        "coverage": trace.coverage,
        "elapsed_ms": trace.elapsed_ms,
    }


def _parse_history(raw: str) -> list[dict[str, str]]:
    if not raw:
        return []
    import json

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [
        {"role": str(row.get("role", "user")), "content": str(row.get("content", ""))}
        for row in data
        if isinstance(row, dict)
    ][:12]
