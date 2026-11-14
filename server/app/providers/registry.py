"""组件注册表：目录、构建、切换、探针。

设计要点
--------
* **目录与实现分离**：``CATALOG`` 只描述「有哪些、要不要密钥、可不可用」，
  构建逻辑在 ``build_components()``，前端只需读目录就能渲染组件中心。
* **切换落盘**：``activate()`` 写 ``var/components.json``，重启仍生效（优先级高于环境变量）。
* **探针不抛错**：``probe()`` 永远返回结构化结果，前端拿它渲染连通性。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from ..config import COMPONENT_KEYS, Settings, get_settings
from .base import (
    DocParser,
    EmbeddingProvider,
    IMChannel,
    LLMProvider,
    Message,
    ObjectStore,
    Reranker,
    VectorStore,
    WebSearch,
)
from .docparser import DefaultMultiFormatParser
from .embedding import LocalHashEmbedding, OpenAICompatEmbedding
from .im import IM_IMPLEMENTATIONS, LogOnlyIM
from .llm import VENDORS, MockLLM, OpenAICompatLLM
from .objectstore import LocalFSObjectStore
from .rerank import CrossEncoderReranker, HeuristicReranker, LLMReranker
from .search import BochaSearch, DuckDuckGoSearch, NoWebSearch, SerperSearch, TavilySearch
from .vectorstore import MemoryVectorStore, PgVectorStore, SqliteNumpyVectorStore


# --------------------------------------------------------------------------- #
# 组件目录（前端「组件中心」直接渲染这个）
# --------------------------------------------------------------------------- #
CATALOG: dict[str, list[dict[str, Any]]] = {
    "llm": [
        {"key": "mock", "display": "本地抽取式（mock）", "needs_key": False,
         "note": "零外呼，可完整跑通问答/本体/Agent/Wiki"},
        *[
            {
                "key": f"openai_compat:{vendor}",
                "display": profile["display"],
                "needs_key": True,
                "note": f"OpenAI 兼容 · 默认模型 {profile['model']}",
            }
            for vendor, profile in VENDORS.items()
        ],
    ],
    "embedding": [
        {"key": "local_hash", "display": "本地特征哈希（384 维）", "needs_key": False,
         "note": "确定性、零依赖；换真实模型后需重建索引"},
        {"key": "openai_compat", "display": "OpenAI 兼容 embeddings", "needs_key": True,
         "note": "百炼 / OpenAI / 自建 bge-m3 等"},
    ],
    "vector_store": [
        {"key": "sqlite_numpy", "display": "SQLite + numpy", "needs_key": False, "note": "默认，十万级片段"},
        {"key": "memory", "display": "进程内存", "needs_key": False, "note": "测试用，重启即失"},
        {"key": "pgvector", "display": "pgvector", "needs_key": True, "note": "需 psycopg 与 vector 扩展"},
    ],
    "object_store": [
        {"key": "local_fs", "display": "本地文件系统", "needs_key": False, "note": "var/objects"},
        {"key": "s3", "display": "S3 / OSS 兼容", "needs_key": True, "note": "需 boto3"},
    ],
    "doc_parser": [
        {"key": "default_multi_format", "display": "内置多格式解析", "needs_key": False,
         "note": "txt/md/csv/json/html/xml/log/docx/xlsx/pdf"},
    ],
    "data_source": [
        {"key": "auto", "display": "按 URI 前缀自动路由", "needs_key": False,
         "note": "local:// file:// sqlite:// http(s):// mysql+pymysql://"},
        {"key": "file", "display": "本地文件", "needs_key": False, "note": "local:// / file://"},
        {"key": "sqlite", "display": "SQLite 表导出", "needs_key": False, "note": "?table=名称&limit=N"},
        {"key": "http_json", "display": "HTTP / JSON", "needs_key": False, "note": "拉取远端文件"},
        {"key": "jdbc", "display": "数据库查询（JDBC 等价）", "needs_key": True, "note": "?sql=SELECT ..."},
    ],
    "web_search": [
        {"key": "none", "display": "关闭（内网默认）", "needs_key": False, "note": "search.* 工具返回已关闭"},
        {"key": "duckduckgo", "display": "DuckDuckGo", "needs_key": False, "note": "免密钥，演示用"},
        {"key": "tavily", "display": "Tavily", "needs_key": True, "note": "结果质量好"},
        {"key": "bocha", "display": "博查", "needs_key": True, "note": "国内可用"},
        {"key": "serper", "display": "Serper", "needs_key": True, "note": "Google 结果"},
    ],
    "im_channel": [
        {"key": "none", "display": "仅日志", "needs_key": False, "note": "通知写到服务日志"},
        {"key": "dingtalk", "display": "钉钉机器人", "needs_key": True, "note": "markdown 消息"},
        {"key": "feishu", "display": "飞书机器人", "needs_key": True, "note": "text 消息"},
        {"key": "wecom", "display": "企业微信机器人", "needs_key": True, "note": "markdown 消息"},
        {"key": "slack", "display": "Slack", "needs_key": True, "note": "incoming webhook"},
    ],
    "rerank": [
        {"key": "heuristic", "display": "启发式（可解释）", "needs_key": False, "note": "覆盖度+密度+权威度"},
        {"key": "llm", "display": "LLM pointwise", "needs_key": True, "note": "让模型给候选打分"},
        {"key": "cross_encoder", "display": "本地 cross-encoder", "needs_key": False,
         "note": "需 sentence-transformers，缺失时自动退回启发式"},
    ],
    "deploy_target": [
        {"key": "local", "display": "本机", "needs_key": False, "note": "python -m app.main"},
        {"key": "docker", "display": "Docker Compose", "needs_key": False, "note": "单镜像 8000"},
        {"key": "k8s", "display": "Kubernetes", "needs_key": False, "note": "deploy/k8s/finonto.yaml"},
        {"key": "serverless", "display": "Serverless / 函数计算", "needs_key": True,
         "note": "需外部 Postgres + 对象存储"},
        {"key": "edge", "display": "边缘节点", "needs_key": True, "note": "本地模型 + 本地存储"},
    ],
}

CATEGORY_LABELS = {
    "llm": "对话/推理模型",
    "embedding": "向量化",
    "vector_store": "向量数据库",
    "object_store": "对象存储",
    "doc_parser": "文档格式",
    "data_source": "数据源",
    "web_search": "网络搜索",
    "im_channel": "IM 渠道",
    "rerank": "重排序",
    "deploy_target": "部署目标",
}

#: 配置项名（Settings 字段 / components.json 的 key）→ 目录 key
COMPONENT_TO_CATALOG: dict[str, str] = {
    "llm_provider": "llm",
    "embedding_provider": "embedding",
    "vector_store": "vector_store",
    "object_store": "object_store",
    "doc_parser": "doc_parser",
    "data_source": "data_source",
    "web_search": "web_search",
    "im_channel": "im_channel",
    "rerank_provider": "rerank",
    "deploy_target": "deploy_target",
}

#: 每个配置项对应的密钥字段（只回显「是否已配置」）
SECRET_BY_COMPONENT: dict[str, str] = {
    "llm_provider": "llm_api_key",
    "embedding_provider": "embedding_api_key",
    "object_store": "object_store_secret",
    "web_search": "web_search_api_key",
    "im_channel": "im_webhook",
}


def _component_key(name: str) -> str:
    """接受 ``llm`` 或 ``llm_provider``，统一成配置项名。"""
    if name in COMPONENT_TO_CATALOG:
        return name
    for component, catalog_key in COMPONENT_TO_CATALOG.items():
        if catalog_key == name:
            return component
    raise ValueError(f"未知组件类别：{name}")


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Components:
    llm: LLMProvider
    embedding: EmbeddingProvider
    vector_store: VectorStore
    object_store: ObjectStore
    doc_parser: DocParser
    web_search: WebSearch
    im: IMChannel
    reranker: Reranker
    selections: dict[str, str] = field(default_factory=dict)


def _build_llm(settings: Settings) -> LLMProvider:
    key = settings.llm_provider or "mock"
    if key.startswith("openai_compat"):
        vendor = key.split(":", 1)[1] if ":" in key else "dashscope"
        return OpenAICompatLLM(
            vendor=vendor,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
        )
    return MockLLM()


def _build_embedding(settings: Settings) -> EmbeddingProvider:
    key = settings.embedding_provider or "local_hash"
    if key.startswith("openai_compat"):
        return OpenAICompatEmbedding(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model or "text-embedding-3-small",
            dim=settings.embedding_dim,
        )
    return LocalHashEmbedding(dim=settings.embedding_dim)


def _build_vector_store(settings: Settings, dim: int) -> VectorStore:
    key = settings.vector_store or "sqlite_numpy"
    if key == "memory":
        return MemoryVectorStore(dim=dim)
    if key == "pgvector":
        return PgVectorStore(settings.resolved_database_url(), dim=dim)
    store = SqliteNumpyVectorStore(dim=dim)
    store.model = settings.embedding_provider or "local_hash"  # type: ignore[attr-defined]
    return store


def _build_object_store(settings: Settings) -> ObjectStore:
    key = settings.object_store or "local_fs"
    if key == "s3":
        from .objectstore import S3CompatibleObjectStore

        return S3CompatibleObjectStore(
            bucket=settings.object_store_bucket,
            endpoint=settings.object_store_endpoint,
            access_key=settings.object_store_key,
            secret_key=settings.object_store_secret,
        )
    return LocalFSObjectStore(settings.objects_dir)


def _build_web_search(settings: Settings) -> WebSearch:
    key = settings.web_search or "none"
    match key:
        case "duckduckgo":
            return DuckDuckGoSearch()
        case "tavily":
            return TavilySearch(settings.web_search_api_key)
        case "bocha":
            return BochaSearch(settings.web_search_api_key)
        case "serper":
            return SerperSearch(settings.web_search_api_key)
        case _:
            return NoWebSearch()


def _build_im(settings: Settings) -> IMChannel:
    key = settings.im_channel or "none"
    factory = IM_IMPLEMENTATIONS.get(key)
    if factory is None or key == "none":
        return LogOnlyIM()
    return factory(settings.im_webhook)


def _build_reranker(settings: Settings, llm: LLMProvider) -> Reranker:
    key = settings.rerank_provider or "heuristic"
    if key == "llm":
        return LLMReranker(llm)
    if key == "cross_encoder":
        return CrossEncoderReranker()
    return HeuristicReranker()


def build_components(settings: Settings | None = None) -> Components:
    settings = settings or get_settings()
    llm = _build_llm(settings)
    embedding = _build_embedding(settings)
    vector_store = _build_vector_store(settings, embedding.dim)
    return Components(
        llm=llm,
        embedding=embedding,
        vector_store=vector_store,
        object_store=_build_object_store(settings),
        doc_parser=DefaultMultiFormatParser(),
        web_search=_build_web_search(settings),
        im=_build_im(settings),
        reranker=_build_reranker(settings, llm),
        selections={key: getattr(settings, key) for key in COMPONENT_KEYS},
    )


@lru_cache(maxsize=1)
def _cached_components(_stamp: int = 0) -> Components:
    return build_components()


def get_components() -> Components:
    return _cached_components()


def reset_components() -> None:
    _cached_components.cache_clear()


# --------------------------------------------------------------------------- #
# 目录 / 切换 / 探针
# --------------------------------------------------------------------------- #
def catalog(settings: Settings | None = None) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    active = {key: getattr(settings, key) for key in COMPONENT_KEYS}
    flags = settings.secret_flags()
    categories: list[dict[str, Any]] = []
    for component_key in COMPONENT_KEYS:
        catalog_key = COMPONENT_TO_CATALOG[component_key]
        secret_field = SECRET_BY_COMPONENT.get(component_key)
        entries = []
        for entry in CATALOG[catalog_key]:
            item = dict(entry)
            item["active"] = entry["key"] == active.get(component_key)
            if entry["needs_key"] and secret_field:
                item["key_configured"] = flags.get(secret_field, False)
            entries.append(item)
        categories.append(
            {
                "key": component_key,
                "catalog_key": catalog_key,
                "label": CATEGORY_LABELS[catalog_key],
                "active": active.get(component_key, ""),
                "entries": entries,
            }
        )
    return categories


def activate(category: str, key: str) -> dict[str, Any]:
    """把某个类别的实现写入 ``var/components.json``（重启后仍生效）。"""
    component_key = _component_key(category)
    catalog_key = COMPONENT_TO_CATALOG[component_key]
    valid = {entry["key"] for entry in CATALOG[catalog_key]}
    if key not in valid:
        raise ValueError(f"{catalog_key} 不支持实现：{key}")
    settings = get_settings()
    path = settings.components_path
    current: dict[str, str] = {}
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            current = {}
    current[component_key] = key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"category": component_key, "key": key, "path": str(path), "restart_required": True}


def probe(category: str, key: str) -> dict[str, Any]:
    """连通性探针：永远返回结构化结果，不抛错。"""
    component_key = _component_key(category)
    category = COMPONENT_TO_CATALOG[component_key]
    settings = get_settings()
    try:
        if category == "llm":
            provider = build_components(settings).llm
            text = provider.chat([Message(role="user", content="ping")], max_tokens=8)
            return {"ok": True, "detail": f"返回 {len(text)} 字符", "provider": provider.info()}
        if category == "embedding":
            provider = build_components(settings).embedding
            vector = provider.embed_one("设备维保")
            return {"ok": True, "detail": f"维度 {len(vector)}", "provider": provider.info()}
        if category == "vector_store":
            store = build_components(settings).vector_store
            return {"ok": True, "detail": str(store.stats()), "provider": store.info()}
        if category == "object_store":
            store = build_components(settings).object_store
            probe_key = "_probe/health.txt"
            store.put(probe_key, b"ok", "text/plain")
            ok = store.get(probe_key) == b"ok"
            store.delete(probe_key)
            return {"ok": ok, "detail": "写入/读取/删除均成功" if ok else "读写不一致", "provider": store.info()}
        if category == "doc_parser":
            parser = build_components(settings).doc_parser
            parsed = parser.parse("probe.txt", "组件探针自检文本。".encode("utf-8"))
            return {"ok": bool(parsed.text), "detail": f"解析到 {len(parsed.text)} 字符", "provider": parser.info()}
        if category == "data_source":
            from .datasource import route

            source = route("local://probe.txt")
            return {"ok": True, "detail": source.info(), "provider": source.info()}
        if category == "web_search":
            search = build_components(settings).web_search
            results = search.search("FinOnto 本体", top_k=2)
            return {
                "ok": True if results or search.name == "none" else False,
                "detail": f"{len(results)} 条结果" if results else "实现已加载（无结果或已关闭）",
                "provider": search.info(),
            }
        if category == "im_channel":
            channel = build_components(settings).im
            ok = channel.send("组件探针：IM 通道连通性测试", title="FinOnto")
            return {"ok": ok, "detail": "已发送" if ok else "发送失败或未配置", "provider": channel.info()}
        if category == "rerank":
            reranker = build_components(settings).reranker
            return {"ok": True, "detail": "实现已加载", "provider": reranker.info()}
        if category == "deploy_target":
            return {"ok": True, "detail": f"当前目标：{key}", "provider": {"name": key}}
        return {"ok": False, "detail": f"未知类别：{category}"}
    except Exception as exc:  # noqa: BLE001 - 探针用于展示，不抛错
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "CATALOG",
    "CATEGORY_LABELS",
    "Components",
    "build_components",
    "catalog",
    "activate",
    "probe",
    "get_components",
    "reset_components",
]
