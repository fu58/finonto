"""组件设置接口：模型/Embedding/向量库/对象存储/文档格式/数据源/IM/网搜/重排/部署的目录、健康检查、切换与连通性探针。"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException

from ..config import get_settings
from ..providers.base import ProviderInfo
from ..providers.registry import probe_llm, registry

router = APIRouter(prefix="/api/components", tags=["components"])

KIND_ORDER = [
    "llm",
    "embedding",
    "vector_store",
    "object_store",
    "doc_parser",
    "data_source",
    "web_search",
    "im_channel",
    "rerank",
    "deploy",
]
KIND_LABELS = {
    "llm": "对话/推理模型",
    "embedding": "向量化模型",
    "vector_store": "向量数据库",
    "object_store": "对象存储",
    "doc_parser": "文档格式解析",
    "data_source": "数据源",
    "web_search": "网络搜索",
    "im_channel": "IM 通知渠道",
    "rerank": "重排序",
    "deploy": "部署目标",
}


def _view(info: ProviderInfo) -> dict[str, Any]:
    return {
        "kind": info.kind,
        "name": info.name,
        "display": info.display,
        "description": info.description,
        "configured": info.configured,
        "local_fallback": info.local_fallback,
        "config_hint": info.config_hint,
        "capabilities": info.capabilities,
        "module": info.module,
    }


def _json_safe(value: Any) -> Any:
    """外部数据源的单元格可能是 BLOB/date/Decimal，直接交给 FastAPI 序列化会 500。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary {len(value)} bytes>"
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _table_view(table: Any) -> dict[str, Any]:
    view = asdict(table)
    view["columns"] = [_json_safe(c) for c in table.columns]
    view["rows"] = [[_json_safe(cell) for cell in row] for row in table.rows]
    view["meta"] = {str(k): _json_safe(v) for k, v in (table.meta or {}).items()}
    return view


def _active_label(kind: str) -> str:
    """没有显式默认项的类别，回退到实际生效的实现或路由策略说明。"""
    active = registry.active_name(kind)
    if active:
        return active
    if kind == "doc_parser":
        return registry.doc_parser().name
    if kind == "data_source":
        return "按 URI 前缀自动路由"
    if kind == "deploy":
        return "由部署清单选择"
    return ""


@router.get("")
def catalog() -> dict[str, Any]:
    """按组件类别分组的全部可选实现，标注当前生效项与是否已配置密钥。"""
    rows = [_view(info) for info in registry.describe()]
    groups = []
    for kind in KIND_ORDER:
        items = [row for row in rows if row["kind"] == kind]
        if not items:
            continue
        groups.append(
            {
                "kind": kind,
                "label": KIND_LABELS.get(kind, kind),
                "active": _active_label(kind),
                "count": len(items),
                "items": items,
            }
        )
    return {"groups": groups, "total": len(rows), "settings": _settings_view()}


def _settings_view() -> dict[str, Any]:
    """只回显密钥是否存在与地址，绝不回传密钥明文。"""
    s = get_settings()
    return {
        "llm_provider": s.llm_provider,
        "llm_base_url": s.llm_base_url,
        "llm_model": s.llm_model,
        "llm_key_present": bool(s.llm_api_key),
        "embedding_provider": s.embedding_provider,
        "embedding_model": s.embedding_model,
        "embedding_key_present": bool(s.embedding_api_key),
        "vector_store": s.vector_store,
        "object_store": s.object_store,
        "web_search_provider": s.web_search_provider,
        "web_search_key_present": bool(s.web_search_api_key),
        "im_channel": s.im_channel,
        "im_webhook_present": bool(s.im_webhook_url),
        "rerank_provider": s.rerank_provider,
        "data_dir": str(s.data_dir),
        "offline_ready": not (s.llm_api_key or s.has_remote_llm),
    }


@router.get("/health")
def health() -> dict[str, Any]:
    return registry.health()


@router.post("/active")
def set_active(body: dict[str, Any]) -> dict[str, Any]:
    """切换某类组件的实现；写入 var/components.json，重启后仍然生效。"""
    kind = str(body.get("kind", ""))
    name = str(body.get("name", ""))
    if kind not in KIND_ORDER:
        raise HTTPException(400, f"未知组件类别 {kind}，可选：{', '.join(KIND_ORDER)}")
    options = [info for info in registry.describe() if info.kind == kind]
    wanted = name.split(":")[-1] if ":" in name else name
    if not any(info.name == name or info.name.split(":")[-1] == wanted for info in options):
        raise HTTPException(
            400,
            f"{kind} 不支持 {name}，可选：" + ", ".join(sorted({i.name for i in options})),
        )
    registry.set_active(kind, name)
    return {"kind": kind, "active": registry.active_name(kind), "health": registry.health()}


@router.post("/probe")
async def probe(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """对某类组件做一次最小真实调用，返回可用性与耗时。"""
    kind = str((body or {}).get("kind", "llm"))
    started = time.perf_counter()
    try:
        if kind == "llm":
            result = await probe_llm()
        elif kind == "embedding":
            emb = registry.embedding()
            vector = emb.embed(["连通性探针"])[0]
            result = {"ok": True, "dim": int(len(vector)), "provider": type(emb).__name__,
                      "preview": [round(float(x), 4) for x in vector[:5]]}
        elif kind == "vector_store":
            store = registry.vector_store()
            ns = f"probe:{int(time.time())}"
            store.upsert(ns, {1: np.zeros(registry.embedding().dim, dtype=np.float32)})
            result = {"ok": True, "class": type(store).__name__, "count": store.count(ns)}
            store.delete(ns, [1])
        elif kind == "object_store":
            obj = registry.object_store()
            key = f"probe/{int(time.time() * 1000)}.txt"
            obj.put(key, b"ping", "text/plain")
            result = {"ok": obj.exists(key), "class": type(obj).__name__, "readback": obj.get(key).decode()}
            obj.delete(key)
        elif kind == "web_search":
            search = registry.web_search()
            results = search.search("本体 领域建模", top_k=3)
            result = {"ok": True, "class": type(search).__name__, "count": len(results),
                      "titles": [r.title[:40] for r in results[:3]]}
        elif kind == "im_channel":
            channel = registry.im()
            delivered = channel.send("ops", "组件连通性探针：这是一条测试通知。", title="FinOnto 探针")
            result = {"ok": True, "class": type(channel).__name__, "delivered": delivered}
        elif kind == "rerank":
            result = {
                "ok": True,
                "active": registry.active_name("rerank"),
                "providers": sorted(i.name for i in registry.describe() if i.kind == "rerank"),
            }
        elif kind == "doc_parser":
            parser = registry.doc_parser()
            parsed = parser.parse("# 标题\n正文".encode(), "md", "probe.md")
            result = {"ok": True, "blocks": len(parsed.blocks), "formats": list(parser.formats)}
        else:
            raise HTTPException(400, f"暂不支持探测 {kind}")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001  探针失败要如实回报而不是 500
        result = {"ok": False, "error": str(exc)[:400]}
    return {"kind": kind, **result, "elapsed_ms": int((time.perf_counter() - started) * 1000)}


@router.post("/im/test")
def im_test(body: dict[str, Any]) -> dict[str, Any]:
    """给 IM 渠道一个真实的发送入口，供提案确认通知复用。"""
    text = str(body.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "text 不能为空")
    channel = registry.im()
    return {
        "channel": getattr(channel, "name", type(channel).__name__),
        "result": channel.send(str(body.get("target", "ops")), text, title=str(body.get("title", "FinOnto 通知"))),
    }


@router.post("/search")
def web_search(body: dict[str, Any]) -> dict[str, Any]:
    query = str(body.get("query", "")).strip()
    if not query:
        raise HTTPException(400, "query 不能为空")
    results = registry.web_search().search(query, top_k=int(body.get("top_k", 5)))
    return {"query": query, "results": [vars(r) for r in results]}


@router.post("/datasource/describe")
def datasource_describe(body: dict[str, Any]) -> dict[str, Any]:
    """按 URI 前缀自动路由到 file/sqlite/http/jdbc 适配器并列出表结构。"""
    uri = str(body.get("uri", "")).strip()
    if not uri:
        raise HTTPException(400, "uri 不能为空")
    source = registry.data_source(uri)
    try:
        tables = source.describe(uri)
    except Exception as exc:  # noqa: BLE001  外部数据源失败要回传原因而不是 500
        return {"uri": uri, "adapter": type(source).__name__, "error": str(exc)[:300], "tables": []}
    return {
        "uri": uri,
        "adapter": type(source).__name__,
        "tables": [_table_view(t) for t in tables],
    }


@router.post("/datasource/sample")
def datasource_sample(body: dict[str, Any]) -> dict[str, Any]:
    uri = str(body.get("uri", "")).strip()
    table = str(body.get("table", "")).strip()
    if not uri or not table:
        raise HTTPException(400, "uri 与 table 必填")
    source = registry.data_source(uri)
    try:
        sampled = source.sample(uri, table, limit=int(body.get("limit", 20)))
    except Exception as exc:  # noqa: BLE001  取数失败回传原因，不能让浏览器拿到 500
        return {
            "uri": uri,
            "table": table,
            "adapter": type(source).__name__,
            "error": str(exc)[:300],
            "sample": None,
        }
    return {"uri": uri, "table": table, "sample": _table_view(sampled)}


@router.get("/deploy")
def deploy_targets() -> dict[str, Any]:
    s = get_settings()
    return {
        "targets": [_view(i) for i in registry.describe() if i.kind == "deploy"],
        "current": {
            "app_name": s.app_name,
            "data_dir": str(s.data_dir),
            "database_url": s.database_url or "sqlite:///<data_dir>/finonto.db",
            "cors_origins": list(s.cors_origins),
        },
    }
