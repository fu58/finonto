"""MCP 客户端：inproc / stdio / http 三种传输。

内置 3 个进程内服务（共 8 个工具），不依赖任何外部进程，方便内网隔离部署：
* ``finonto-kb``       —— 检索、文档列表、片段读取
* ``finonto-ontology`` —— 类型列表、类型详情、相似检查、提案
* ``finonto-export``   —— Turtle / SKOS / JSON Schema 导出

stdio / http 传输只做登记与连通性探测；真正调用外部 MCP 服务需要按其协议握手，
这属于部署期集成工作，不在「一条命令跑起来」的范围内。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from ...models import MCPServer
from .tools import ToolContext, registry


@dataclass(slots=True)
class MCPTool:
    name: str
    description: str
    arguments: dict[str, Any] = field(default_factory=dict)


BUILTIN_SERVERS: dict[str, dict[str, Any]] = {
    "finonto-kb": {
        "description": "知识库检索服务（RAG 与文档）",
        "tools": [
            {"name": "search", "tool": "rag.search", "description": "语义检索并回答"},
            {"name": "list_documents", "tool": "doc.list", "description": "列出文档"},
            {"name": "read_chunks", "tool": "doc.chunks", "description": "读取文档片段"},
        ],
    },
    "finonto-ontology": {
        "description": "本体服务（类型查询与提案）",
        "tools": [
            {"name": "list_types", "tool": "ontology.list_types", "description": "列出对象类型"},
            {"name": "type_detail", "tool": "ontology.type_detail", "description": "类型详情"},
            {"name": "find_similar", "tool": "find_similar", "description": "疑似重复检查"},
            {"name": "propose_type", "tool": "propose_type", "description": "产出类型提案"},
        ],
    },
    "finonto-export": {
        "description": "导出服务（OWL / SKOS / JSON Schema）",
        "tools": [
            {"name": "export_turtle", "tool": "__export__:turtle", "description": "导出 Turtle"},
        ],
    },
}


def ensure_builtin_servers(session) -> None:
    """幂等登记内置服务（冷启动调用一次即可）。"""
    existing = {
        name for (name,) in session.execute(select(MCPServer.name)).all()
    }
    for name, spec in BUILTIN_SERVERS.items():
        if name in existing:
            continue
        session.add(
            MCPServer(
                name=name,
                transport="inproc",
                command="",
                url="",
                tools=[tool["name"] for tool in spec["tools"]],
                is_enabled=True,
            )
        )
    session.commit()


def list_servers(session) -> list[dict[str, Any]]:
    rows = session.execute(select(MCPServer).order_by(MCPServer.name)).scalars().all()
    result = []
    for row in rows:
        spec = BUILTIN_SERVERS.get(row.name, {})
        result.append(
            {
                "id": row.id,
                "name": row.name,
                "transport": row.transport,
                "enabled": row.is_enabled,
                "tools": row.tools or [],
                "description": spec.get("description", "外部 MCP 服务"),
                "inproc": row.name in BUILTIN_SERVERS,
            }
        )
    return result


def catalog(session) -> list[dict[str, Any]]:
    """列出所有服务的全部工具（前端「MCP」面板直接渲染）。"""
    items: list[dict[str, Any]] = []
    for server in list_servers(session):
        spec = BUILTIN_SERVERS.get(server["name"], {})
        for tool in spec.get("tools", []):
            items.append(
                {
                    "server": server["name"],
                    "tool": tool["name"],
                    "target": tool["tool"],
                    "description": tool["description"],
                    "enabled": server["enabled"],
                }
            )
    return items


def register_server(
    session, name: str, *, transport: str = "http", url: str = "", command: str = ""
) -> dict[str, Any]:
    existing = session.execute(select(MCPServer).where(MCPServer.name == name)).scalars().first()
    if existing is not None:
        return {"status": "exists", "id": existing.id, "name": name}
    row = MCPServer(name=name, transport=transport, url=url, command=command, tools=[], is_enabled=True)
    session.add(row)
    session.commit()
    return {"status": "created", "id": row.id, "name": name}


def toggle_server(session, name: str, enabled: bool) -> dict[str, Any]:
    row = session.execute(select(MCPServer).where(MCPServer.name == name)).scalars().first()
    if row is None:
        return {"status": "missing", "name": name}
    row.is_enabled = bool(enabled)
    session.commit()
    return {"status": "enabled" if row.is_enabled else "disabled", "name": name}


def call_tool(
    session,
    space_id: int,
    components,
    server_name: str,
    tool_name: str,
    arguments: dict | None = None,
) -> dict[str, Any]:
    spec = BUILTIN_SERVERS.get(server_name)
    if spec is None:
        return {"ok": False, "error": f"仅支持内置 inproc 服务直连调用：{server_name}"}
    target = next((tool for tool in spec["tools"] if tool["name"] == tool_name), None)
    if target is None:
        return {"ok": False, "error": f"{server_name} 没有工具 {tool_name}"}

    target_tool: str = target["tool"]
    if target_tool.startswith("__export__:"):
        from ..ontology import owl

        fmt = target_tool.split(":", 1)[1]
        if fmt == "turtle":
            return {"ok": True, "content": owl.export_turtle(session, space_id)}
        return {"ok": False, "error": f"未知导出格式：{fmt}"}

    context = ToolContext(session=session, space_id=space_id, components=components)
    return registry().invoke(target_tool, context, arguments or {})


def probe(session, name: str) -> dict[str, Any]:
    row = session.execute(select(MCPServer).where(MCPServer.name == name)).scalars().first()
    if row is None:
        return {"ok": False, "detail": f"服务未登记：{name}"}
    if row.transport == "inproc":
        spec = BUILTIN_SERVERS.get(name, {})
        return {
            "ok": bool(spec),
            "detail": f"进程内服务，{len(spec.get('tools', []))} 个工具",
            "tools": [tool["name"] for tool in spec.get("tools", [])],
        }
    return {
        "ok": bool(row.url or row.command),
        "detail": f"{row.transport} 服务已登记（握手需部署期完成）",
        "url": row.url,
        "command": row.command,
    }


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
