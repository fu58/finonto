"""Agent 接口：ReAct 推理（流式/非流式）、运行历史、技能与工具目录、长期记忆、MCP、沙箱。"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..core.agent import memory as memory_mod
from ..core.agent import react
from ..core.agent.mcp_client import MCPServerConfig, mcp_manager
from ..core.agent.memory import MEMORY_KINDS, MemoryService
from ..core.agent.sandbox import run_python, validate_source
from ..core.agent.skills import get_skill
from ..core.agent.tools import ToolContext, register_mcp_tools, registry_tools
from ..db import get_session
from .sse import sse_response

router = APIRouter(prefix="/api/agent", tags=["agent"])


# ------------------------------------------------------------------ 目录
@router.get("/skills")
def list_skills() -> list[dict[str, Any]]:
    """技能包：系统提示 + 工具白名单 + 输出契约 + 步数上限。"""
    return react.skill_catalog()


@router.get("/tools")
def list_tools(space_id: int = 1, skill: str | None = None) -> dict[str, Any]:
    """工具面目录；给定 skill 时返回该技能实际可见的工具。"""
    register_mcp_tools()
    allowed = list(get_skill(skill).allowed_tools) if get_skill(skill) else None
    if allowed:
        allowed = allowed + [t.qualified for t in mcp_manager.tools()]
    tools = registry_tools.tools(allowed)
    return {
        "space_id": space_id,
        "skill": skill or "",
        "builtin": [tool.schema() for tool in tools if not tool.name.startswith("mcp.")],
        "mcp": [tool.schema() for tool in tools if tool.name.startswith("mcp.")],
        "risk_summary": _risk_summary(tools),
    }


def _risk_summary(tools: list[Any]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for tool in tools:
        summary[tool.risk] = summary.get(tool.risk, 0) + 1
    return summary


# ------------------------------------------------------------------ 推理
@router.get("/stream")
async def stream_run(
    goal: str,
    space_id: int = 1,
    skill: str | None = None,
    max_steps: int | None = None,
    use_memory: bool = True,
    include_mcp: bool = True,
    session: Session = Depends(get_session),
):
    """SSE 流：run_start → step → observation → proposal → final。"""
    if not goal.strip():
        raise HTTPException(400, "goal 不能为空")
    engine = react.ReActEngine(
        session,
        space_id,
        react.ReActConfig(
            skill=skill,
            max_steps=max_steps,
            use_memory=use_memory,
            include_mcp=include_mcp,
        ),
    )
    return sse_response(engine.run_stream(goal))


@router.post("/run")
async def run(body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    """非流式推理：一次拿到最终答案与完整事件轨迹。"""
    goal = str(body.get("goal", "")).strip()
    if not goal:
        raise HTTPException(400, "goal 不能为空")
    engine = react.ReActEngine(
        session,
        int(body.get("space_id", 1)),
        react.ReActConfig(
            skill=body.get("skill"),
            max_steps=body.get("max_steps"),
            use_memory=bool(body.get("use_memory", True)),
            include_mcp=bool(body.get("include_mcp", True)),
        ),
    )
    result = await engine.run(goal, session_id=body.get("session_id"))
    return {k: v for k, v in result.items() if k != "events"} | {
        "events": _compact_events(result.get("events", []))
    }


def _compact_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 observation 文本截短，避免一次非流式调用把响应体撑爆。"""
    out = []
    for event in events:
        if event.get("type") == "observation":
            payload = dict(event)
            payload["observation"] = str(payload.get("observation", ""))[:2000]
            out.append(payload)
        else:
            out.append(event)
    return out


@router.get("/runs")
def runs(space_id: int | None = None, limit: int = 30, session: Session = Depends(get_session)):
    return {"runs": react.list_runs(session, space_id, min(max(limit, 1), 200))}


@router.get("/runs/{run_id}")
def run_detail(run_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    detail = react.load_run(session, run_id)
    if detail is None:
        raise HTTPException(404, f"运行 {run_id} 不存在")
    return detail


# ------------------------------------------------------------------ 长期记忆
@router.get("/memory")
def memory_list(
    scope: str | None = None,
    limit: int = 200,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    service = MemoryService(session)
    return {
        "items": [
            {
                "id": item.id,
                "kind": item.kind,
                "scope": item.scope,
                "content": item.content,
                "keywords": item.keywords or [],
                "salience": round(item.salience, 3),
                "use_count": item.use_count,
                "source_run_id": item.source_run_id,
                "created_at": item.created_at.isoformat() if item.created_at else "",
            }
            for item in service.list_items(scope, min(max(limit, 1), 1000))
        ],
        "stats": service.stats(),
        "kinds": list(MEMORY_KINDS),
    }


@router.post("/memory")
def memory_remember(body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    content = str(body.get("content", "")).strip()
    if len(content) < 4:
        raise HTTPException(400, "content 至少 4 个字符")
    item = MemoryService(session).remember(
        content,
        scope=str(body.get("scope", "global")),
        kind=str(body.get("kind", "fact")),
        keywords=body.get("keywords") or None,
        salience=float(body.get("salience", 1.0)),
    )
    if item is None:
        raise HTTPException(400, "记忆写入失败")
    return {"id": item.id, "kind": item.kind, "salience": item.salience, "deduped": False}


@router.post("/memory/recall")
def memory_recall(body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    query = str(body.get("query", "")).strip()
    if not query:
        raise HTTPException(400, "query 不能为空")
    hits = MemoryService(session).recall(
        query,
        scope=body.get("scope"),
        kinds=body.get("kinds"),
        top_k=int(body.get("top_k", 5)),
        reinforce=bool(body.get("reinforce", True)),
    )
    return {"query": query, "hits": [hit.as_dict() for hit in hits]}


@router.delete("/memory/{item_id}")
def memory_forget(item_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    if not MemoryService(session).forget(item_id):
        raise HTTPException(404, f"记忆 {item_id} 不存在")
    return {"forgotten": item_id}


@router.post("/memory/decay")
def memory_decay(half_life_days: float = 45.0, session: Session = Depends(get_session)) -> dict[str, Any]:
    changed = MemoryService(session).decay(half_life_days=half_life_days)
    return {"updated": changed, "half_life_days": half_life_days}


@router.post("/memory/prune")
def memory_prune(
    older_than_days: int = 180,
    min_salience: float = 0.2,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return {"removed": memory_mod.prune(session, older_than_days=older_than_days, min_salience=min_salience)}


@router.post("/memory/consolidate")
async def memory_consolidate(run_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    item = await MemoryService(session).consolidate_run(run_id)
    if item is None:
        raise HTTPException(400, "该运行没有可沉淀的内容（可能已存在或缺少最终答案）")
    return {"id": item.id, "kind": item.kind, "content": item.content, "salience": item.salience}


# ------------------------------------------------------------------ MCP
@router.get("/mcp/servers")
def mcp_servers() -> list[dict[str, Any]]:
    return mcp_manager.describe()


@router.post("/mcp/servers")
def mcp_add(body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "name 不能为空")
    transport = str(body.get("transport", "stdio"))
    if transport not in ("inproc", "stdio", "http"):
        raise HTTPException(400, "transport 仅支持 inproc / stdio / http")
    if transport == "stdio" and not body.get("command"):
        raise HTTPException(400, "stdio 传输需要 command")
    if transport == "http" and not body.get("url"):
        raise HTTPException(400, "http 传输需要 url")
    mcp_manager.add_server(
        MCPServerConfig(
            name=name,
            display=str(body.get("display", "")),
            transport=transport,
            command=str(body.get("command", "")),
            args=list(body.get("args") or []),
            url=str(body.get("url", "")),
            enabled=bool(body.get("enabled", True)),
            headers=dict(body.get("headers") or {}),
            description=str(body.get("description", "")),
        )
    )
    register_mcp_tools()
    return {"added": name, "servers": len(mcp_manager.list_servers())}


@router.post("/mcp/servers/{name}/enabled")
def mcp_toggle(name: str, enabled: bool = True) -> dict[str, Any]:
    if not any(c.name == name for c in mcp_manager.list_servers()):
        raise HTTPException(404, f"MCP 服务 {name} 不存在")
    mcp_manager.set_enabled(name, enabled)
    register_mcp_tools()
    return {"name": name, "enabled": enabled}


@router.delete("/mcp/servers/{name}")
def mcp_remove(name: str) -> dict[str, Any]:
    if not mcp_manager.remove_server(name):
        raise HTTPException(404, f"MCP 服务 {name} 不存在")
    register_mcp_tools()
    return {"removed": name}


@router.get("/mcp/tools")
def mcp_tools() -> list[dict[str, Any]]:
    return [
        {"name": t.name, "qualified": t.qualified, "server": t.server, "description": t.description, "parameters": t.parameters}
        for t in mcp_manager.tools()
    ]


@router.post("/mcp/call")
def mcp_call(body: dict[str, Any]) -> dict[str, Any]:
    server = str(body.get("server", "")).strip()
    tool = str(body.get("tool", "")).strip()
    if not server or not tool:
        raise HTTPException(400, "server 与 tool 必填")
    arguments = body.get("arguments") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"arguments 不是合法 JSON: {exc}") from exc
    return mcp_manager.call(server, tool, dict(arguments))


# ------------------------------------------------------------------ 沙箱
@router.post("/sandbox/validate")
def sandbox_validate(body: dict[str, Any]) -> dict[str, Any]:
    code = str(body.get("code", ""))
    violations = validate_source(code)
    return {"ok": not violations, "violations": violations}


@router.post("/sandbox")
def sandbox_run(body: dict[str, Any]) -> dict[str, Any]:
    """在受限子进程中执行 Python（AST 白名单 + 超时 + 输出上限）。"""
    code = str(body.get("code", ""))
    if not code.strip():
        raise HTTPException(400, "code 不能为空")
    result = run_python(
        code,
        timeout=float(body["timeout"]) if body.get("timeout") else None,
        stdin_data=str(body.get("stdin", "")),
    )
    return asdict(result)


@router.get("/sandbox/config")
def sandbox_config() -> dict[str, Any]:
    from ..config import settings
    from ..core.agent.sandbox import ALLOWED_IMPORTS

    return {
        "enabled": settings.sandbox_enabled,
        "timeout_s": settings.sandbox_timeout_s,
        "max_output": settings.sandbox_max_output,
        "allowed_imports": sorted(ALLOWED_IMPORTS),
    }


# ------------------------------------------------------------------ 单工具直调（调试用）
@router.post("/tools/{tool_name}/execute")
async def execute_tool(
    tool_name: str,
    body: dict[str, Any],
    space_id: int = 1,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """跳过推理循环，直接执行一个工具。用于前端“试跑”某个动作。"""
    if not registry_tools.has(tool_name):
        raise HTTPException(404, f"工具 {tool_name} 不存在")
    ctx = ToolContext(session=session, space_id=space_id, skill=body.get("skill"))
    arguments = body.get("arguments") or {}
    return await registry_tools.execute(ctx, tool_name, dict(arguments))
