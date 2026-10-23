"""MCP（Model Context Protocol）接入。

三种传输：
- `inproc` —— 平台内置的 MCP 服务（把知识库/本体/导出能力按 MCP 工具语义再暴露一次），
  零依赖即可演示「工具 / 技能 / MCP」三类扩展点；
- `stdio` —— 启动本地 MCP server 进程（需 `pip install mcp`）；
- `http`  —— 连接远端 MCP streamable-http 服务。

服务器配置持久化在 var/mcp_servers.json，前端可增删并逐个探测工具。
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from ...config import settings
from ...core.text import truncate

INPROC_SERVERS: dict[str, dict[str, dict[str, Any]]] = {
    "finonto-kb": {
        "kb.list_docs": {
            "description": "列出知识库中已入库的文档（名称、格式、切块数、权威度）。",
            "parameters": {"space_id": {"type": "integer", "required": False}},
        },
        "kb.search": {
            "description": "在知识库中做混合检索，返回带定位的原文片段。",
            "parameters": {"query": {"type": "string", "required": True}, "top_k": {"type": "integer", "required": False}},
        },
        "kb.read_chunk": {
            "description": "按 chunk_id 读取原文片段全文。",
            "parameters": {"chunk_id": {"type": "integer", "required": True}},
        },
    },
    "finonto-ontology": {
        "onto.list_types": {
            "description": "列出本体空间中的对象类型。",
            "parameters": {"space_id": {"type": "integer", "required": True}},
        },
        "onto.get_type": {
            "description": "读取对象类型完整详情（属性、关联、约束、证据）。",
            "parameters": {"type_id": {"type": "integer", "required": True}},
        },
        "onto.validate": {
            "description": "对本体空间做一致性校验，返回问题清单。",
            "parameters": {"space_id": {"type": "integer", "required": True}},
        },
    },
    "finonto-export": {
        "export.turtle": {
            "description": "把本体空间导出为 OWL Turtle 文本。",
            "parameters": {"space_id": {"type": "integer", "required": True}},
        },
        "export.jsonschema": {
            "description": "把某个对象类型导出为 JSON Schema。",
            "parameters": {"space_id": {"type": "integer", "required": True}, "type_id": {"type": "integer", "required": True}},
        },
    },
}


@dataclass
class MCPServerConfig:
    name: str
    display: str = ""
    transport: str = "inproc"
    command: str = ""
    args: list[str] = field(default_factory=list)
    url: str = ""
    enabled: bool = True
    headers: dict[str, str] = field(default_factory=dict)
    description: str = ""


@dataclass
class MCPTool:
    server: str
    name: str
    description: str
    parameters: dict[str, Any]

    @property
    def qualified(self) -> str:
        return f"mcp.{self.server}.{self.name}"


class MCPManager:
    def __init__(self) -> None:
        self._path = settings.data_dir / "mcp_servers.json"
        self._lock = threading.RLock()
        self._configs: list[MCPServerConfig] = self._load()
        self._handlers: dict[str, Callable[[dict], dict]] = {}

    # ------------------------------------------------------------ 配置
    def _load(self) -> list[MCPServerConfig]:
        rows: list[MCPServerConfig] = [
            MCPServerConfig(
                name=name,
                display=f"内置 MCP 服务 · {name}",
                transport="inproc",
                description="平台内置，无需外部进程；用于演示 MCP 工具发现与调用。",
            )
            for name in INPROC_SERVERS
        ]
        if self._path.exists():
            try:
                payload = json.loads(self._path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return rows
            for entry in payload:
                if entry.get("name") in {r.name for r in rows}:
                    rows = [r if r.name != entry["name"] else MCPServerConfig(**entry) for r in rows]
                    continue
                rows.append(MCPServerConfig(**entry))
        return rows

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        external = [asdict(c) for c in self._configs if c.transport != "inproc"]
        self._path.write_text(json.dumps(external, ensure_ascii=False, indent=2), encoding="utf-8")

    def list_servers(self) -> list[MCPServerConfig]:
        return list(self._configs)

    def add_server(self, config: MCPServerConfig) -> None:
        with self._lock:
            self._configs = [c for c in self._configs if c.name != config.name] + [config]
            self._save()

    def remove_server(self, name: str) -> bool:
        with self._lock:
            before = len(self._configs)
            if name in INPROC_SERVERS:
                self._configs = [
                    c if c.name != name else MCPServerConfig(name=name, display=f"内置 MCP 服务 · {name}", transport="inproc", enabled=False)
                    for c in self._configs
                ]
                self._save()
                return True
            self._configs = [c for c in self._configs if c.name != name]
            self._save()
            return len(self._configs) < before

    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            for config in self._configs:
                if config.name == name:
                    config.enabled = enabled
            self._save()

    # ------------------------------------------------------------ 工具
    def tools(self) -> list[MCPTool]:
        out: list[MCPTool] = []
        for config in self._configs:
            if not config.enabled:
                continue
            if config.transport == "inproc":
                for name, spec in INPROC_SERVERS.get(config.name, {}).items():
                    out.append(MCPTool(config.name, name, spec["description"], spec["parameters"]))
            else:
                out.extend(self._remote_tools(config))
        return out

    def _remote_tools(self, config: MCPServerConfig) -> list[MCPTool]:
        client = self._build_client(config)
        if client is None:
            return []
        try:
            listing = client.list_tools()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return []
        return [
            MCPTool(
                config.name,
                tool.get("name", ""),
                tool.get("description", ""),
                {
                    key: {"type": schema.get("type", "string"), "required": key in (schema.get("required") or [])}
                    for key, schema in ((tool.get("inputSchema") or {}).get("properties", {}) or {}).items()
                },
            )
            for tool in listing
        ]

    def _build_client(self, config: MCPServerConfig) -> Any:
        try:
            import mcp  # type: ignore  # noqa: F401
        except ImportError:
            return None
        if config.transport == "stdio" and config.command:
            from mcp.client.stdio import StdioServerParameters  # pragma: no cover

            return _StdioClient(StdioServerParameters(command=config.command, args=config.args))
        if config.transport == "http" and config.url:
            return _HttpClient(config.url, config.headers)
        return None

    def register_inproc_handler(self, qualified_name: str, handler: Callable[[dict], dict]) -> None:
        self._handlers[qualified_name] = handler

    def call(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        config = next((c for c in self._configs if c.name == server), None)
        if config is None or not config.enabled:
            return {"ok": False, "error": f"MCP 服务 {server} 未启用或未注册"}
        qualified = f"mcp.{server}.{tool}"
        if config.transport == "inproc":
            handler = self._handlers.get(qualified)
            if handler is None:
                return {"ok": False, "error": f"内置工具 {qualified} 没有绑定处理函数"}
            try:
                return {"ok": True, "result": handler(arguments)}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)[:300]}
        client = self._build_client(config)
        if client is None:
            return {
                "ok": False,
                "error": "未安装官方 mcp 客户端，请 pip install mcp 后重试（stdio/http 传输需要）",
            }
        try:
            return {"ok": True, "result": client.call_tool(tool, arguments)}  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:300]}

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                **asdict(config),
                "tools": [
                    {
                        "name": t.name,
                        "qualified": t.qualified,
                        "description": t.description,
                        "parameters": t.parameters,
                    }
                    for t in self.tools()
                    if t.server == config.name
                ],
            }
            for config in self._configs
        ]


class _StdioClient:  # pragma: no cover - 需要外部 mcp 包与 server
    def __init__(self, params: Any) -> None:
        self.params = params

    def list_tools(self) -> list[dict]:
        import asyncio

        from mcp import ClientSession, stdio_client

        async def _run() -> list[dict]:
            async with stdio_client(self.params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return [asdict(t) for t in (await session.list_tools()).tools]

        return asyncio.run(_run())

    def call_tool(self, name: str, arguments: dict) -> dict:
        import asyncio

        from mcp import ClientSession, stdio_client

        async def _run() -> dict:
            async with stdio_client(self.params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
                    return {"content": [getattr(c, "text", str(c)) for c in result.content]}

        return asyncio.run(_run())


class _HttpClient:  # pragma: no cover - 需要外部 mcp 包
    def __init__(self, url: str, headers: dict[str, str]) -> None:
        self.url = url
        self.headers = headers

    def list_tools(self) -> list[dict]:
        import asyncio

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def _run() -> list[dict]:
            async with streamablehttp_client(self.url, headers=self.headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return [asdict(t) for t in (await session.list_tools()).tools]

        return asyncio.run(_run())

    def call_tool(self, name: str, arguments: dict) -> dict:
        import asyncio

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def _run() -> dict:
            async with streamablehttp_client(self.url, headers=self.headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
                    return {"content": [truncate(getattr(c, "text", str(c)), 4000) for c in result.content]}

        return asyncio.run(_run())


mcp_manager = MCPManager()
