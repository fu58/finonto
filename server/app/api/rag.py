"""问答接口：SSE 流式 + 同步一次性 + 轨迹回放。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.rag.pipeline import ask, ask_stream
from ..db import get_db
from ..models import QueryTrace
from .sse import sse_response

router = APIRouter(prefix="/api/rag", tags=["rag"])


@router.get("/stream")
def stream(
    space_id: int = Query(..., alias="space"),
    q: str = Query(..., description="问题"),
    top_k: int = Query(8),
):
    """SSE：plan → retrieval → token* → citations → done。"""
    return sse_response(ask_stream(space_id, q, top_k=top_k))


@router.post("/ask")
def ask_once(
    space_id: int = Query(..., alias="space"),
    q: str = Query(...),
    top_k: int = Query(8),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ask(db, space_id, q, top_k=top_k)


@router.get("/traces")
def traces(
    space_id: int = Query(..., alias="space"),
    limit: int = Query(20),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = db.execute(
        select(QueryTrace)
        .where(QueryTrace.space_id == space_id)
        .order_by(QueryTrace.id.desc())
        .limit(limit)
    ).scalars().all()
    return {
        "traces": [
            {
                "id": row.id,
                "question": row.question,
                "intent": row.intent,
                "provider": row.provider,
                "latency_ms": row.latency_ms,
                "citations": len(row.citations or []),
                "created_at": row.created_at.isoformat(sep=" ", timespec="seconds"),
            }
            for row in rows
        ]
    }


@router.get("/traces/{trace_id}")
def trace_detail(trace_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(QueryTrace, trace_id)
    if row is None:
        return {"status": "missing", "trace_id": trace_id}
    return {
        "id": row.id,
        "question": row.question,
        "intent": row.intent,
        "sub_questions": row.sub_questions,
        "filters": row.filters,
        "answer": row.answer,
        "citations": row.citations,
        "retrieved": row.retrieved,
        "provider": row.provider,
        "latency_ms": row.latency_ms,
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds"),
    }
