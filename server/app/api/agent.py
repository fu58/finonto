"""Agent 接口：ReAct 流式、运行记录、技能、沙箱、记忆、MCP。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.agent import mcp_client, memory as memory_service, react, sandbox, skills
from ..core.agent.tools import registry
from ..db import get_db
from ..models import AgentRun, AgentStep
from .sse import sse_response

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.get("/stream")
def stream(
    space_id: int = Query(..., alias="space"),
    goal: str = Query(...),
    skill: str = Query("doc-extraction"),
    max_steps: int = Query(0),
):
    return sse_response(react.run_stream(space_id, goal, skill_key=skill, max_steps=max_steps or None))


@router.post("/run")
def run_once(
    space_id: int = Query(..., alias="space"),
    goal: str = Query(...),
    skill: str = Query("doc-extraction"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = react.run(db, space_id, goal, skill_key=skill)
    return result.as_dict()


@router.get("/skills")
def list_skills() -> dict[str, Any]:
    return {"skills": skills.catalog()}


@router.get("/tools")
def list_tools() -> dict[str, Any]:
    return {"tools": registry().catalog()}


@router.get("/runs")
def list_runs(space_id: int = Query(..., alias="space"), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(
        select(AgentRun).where(AgentRun.space_id == space_id).order_by(AgentRun.id.desc()).limit(30)
    ).scalars().all()
    return {
        "runs": [
            {
                "id": row.id,
                "skill": row.skill,
                "goal": row.goal,
                "status": row.status,
                "steps": row.step_count,
                "tokens": row.token_count,
                "proposals": row.proposal_ids or [],
                "conclusion": row.conclusion,
                "created_at": row.created_at.isoformat(sep=" ", timespec="seconds"),
            }
            for row in rows
        ]
    }


@router.get("/runs/{run_id}")
def run_detail(run_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    steps = db.execute(
        select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.ordinal)
    ).scalars().all()
    return {
        "id": run.id,
        "skill": run.skill,
        "goal": run.goal,
        "status": run.status,
        "conclusion": run.conclusion,
        "proposal_ids": run.proposal_ids or [],
        "tokens": run.token_count,
        "steps": [
            {
                "ordinal": step.ordinal,
                "thought": step.thought,
                "action": step.action,
                "action_input": step.action_input,
                "observation": step.observation,
                "elapsed_ms": step.elapsed_ms,
            }
            for step in steps
        ],
    }


@router.post("/sandbox")
def run_sandbox(payload: dict = Body(...)) -> dict[str, Any]:
    return sandbox.run(payload.get("code", ""))


@router.post("/sandbox/validate")
def validate_sandbox(payload: dict = Body(...)) -> dict[str, Any]:
    return sandbox.validate(payload.get("code", ""))


@router.get("/memory")
def list_memory(space_id: int = Query(..., alias="space"), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {
        "memories": memory_service.list_memories(db, space_id),
        "stats": memory_service.stats(db, space_id),
    }


@router.post("/memory")
def save_memory(
    space_id: int = Query(..., alias="space"),
    payload: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    content = (payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content 不能为空")
    item = memory_service.save(
        db, space_id, content, kind=payload.get("kind", "fact"), keywords=payload.get("keywords", "")
    )
    db.commit()
    return {"status": "saved", "id": item.id}


@router.post("/memory/decay")
def decay_memory(space_id: int = Query(..., alias="space"), db: Session = Depends(get_db)) -> dict[str, Any]:
    changed = memory_service.decay(db, space_id)
    db.commit()
    return {"status": "decayed", "changed": changed}


@router.post("/memory/prune")
def prune_memory(space_id: int = Query(..., alias="space"), db: Session = Depends(get_db)) -> dict[str, Any]:
    removed = memory_service.prune(db, space_id)
    db.commit()
    return {"status": "pruned", "removed": removed}


@router.get("/mcp")
def list_mcp(db: Session = Depends(get_db)) -> dict[str, Any]:
    mcp_client.ensure_builtin_servers(db)
    return {"servers": mcp_client.list_servers(db), "tools": mcp_client.catalog(db)}


@router.post("/mcp")
def create_mcp(payload: dict = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 必填")
    return mcp_client.register_server(
        db,
        name,
        transport=payload.get("transport", "http"),
        url=payload.get("url", ""),
        command=payload.get("command", ""),
    )


@router.post("/mcp/{name}/toggle")
def toggle_mcp(name: str, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    return mcp_client.toggle_server(db, name, bool(payload.get("enabled", True)))


@router.get("/mcp/{name}/probe")
def probe_mcp(name: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return mcp_client.probe(db, name)


@router.post("/mcp/{name}/call")
def call_mcp(
    name: str,
    space_id: int = Query(..., alias="space"),
    payload: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from ..providers.registry import get_components

    return mcp_client.call_tool(
        db, space_id, get_components(), name, payload.get("tool", ""), payload.get("arguments") or {}
    )
