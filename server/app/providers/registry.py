"""组件注册表：按配置装配 Provider，支持运行时切换并持久化选择。

`describe()` 给出全部可选组件（含未配置的厂商 / 向量库 / 存储 / 渠道），
供前端「组件设置」页展示与切换；`切换后的实现` 写入 var/components.json，
重启仍然生效，且不会改动 .env。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import numpy as np

from ..config import Settings, get_settings
from .base import (
    ChatMessage,
    DocParser,
    EmbeddingProvider,
    IMChannel,
    LLMProvider,
    ObjectStore,
    ProviderInfo,
    VectorHit,
    VectorStore,
    WebSearch,
)
from .datasource.connectors import (
    DataSource,
    FileDataSource,
    HttpJsonDataSource,
    JdbcDataSource,
    SQLiteDataSource,
)
from .docparser.parsers import DefaultDocParser
from .embedding.local_hash import LocalHashEmbedder
from .embedding.openai_compat import OpenAICompatEmbedder
from .im.channels import (
    ConsoleChannel,
    DingTalkChannel,
    FeishuChannel,
    SlackChannel,
    WeComChannel,
)
from .llm.mock import MockLLMProvider
from .llm.openai_compat import OpenAICompatProvider
from .objectstore.stores import LocalFSObjectStore, OSSObjectStore
from .search.web import ApiKeyWebSearch, DuckDuckGoSearch, NoopWebSearch
from .vectorstore.external import DashVectorStore, PgVectorStore
from .vectorstore.sqlite_numpy import SqliteNumpyVectorStore

LLM_VENDORS: list[dict[str, str]] = [
    {"key": "dashscope", "display": "阿里云百炼 / 通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    {"key": "openai", "display": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    {"key": "deepseek", "display": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    {"key": "moonshot", "display": "月之暗面 Kimi", "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-32k"},
    {"key": "zhipu", "display": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-plus"},
    {"key": "volc", "display": "火山方舟豆包", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-pro-32k"},
    {"key": "vllm", "display": "自建 vLLM / OpenAI 兼容网关", "base_url": "http://127.0.0.1:8000/v1", "model": "Qwen2.5-32B-Instruct"},
    {"key": "ollama", "display": "本机 Ollama", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:14b"},
]

DEPLOY_TARGETS: list[dict[str, str]] = [
    {"key": "local", "display": "本机 uvicorn + Vite dev", "hint": "scripts/dev.sh（Windows 用 scripts\\dev.ps1）"},
    {"key": "docker", "display": "Docker Compose 单机", "hint": "docker compose up -d，见 docker-compose.yml"},
    {"key": "k8s", "display": "Kubernetes / Helm", "hint": "kubectl apply -f deploy/k8s/finonto.yaml（Deployment + Service + PVC）"},
    {"key": "serverless", "display": "函数计算 / Serverless", "hint": "需要外部 Postgres + OSS + NAS，见 README「部署形态」"},
    {"key": "edge", "display": "内网隔离部署（零外呼）", "hint": "provider 全用本地实现 + 自建 vLLM，见 README「部署形态」"},
]

_STATE_LOCK = threading.RLock()


class InMemoryVectorStore:
    """供测试与临时命名空间使用。"""

    name = "memory"

    def __init__(self) -> None:
        self._data: dict[str, dict[int, tuple[np.ndarray, dict]]] = {}

    def upsert(self, namespace: str, vectors: dict[int, np.ndarray], meta: dict[int, dict] | None = None) -> int:
        meta = meta or {}
        bucket = self._data.setdefault(namespace, {})
        for ref_id, vec in vectors.items():
            bucket[ref_id] = (np.asarray(vec, dtype=np.float32), meta.get(ref_id, {}))
        return len(vectors)

    def delete(self, namespace: str, ref_ids: list[int]) -> int:
        bucket = self._data.get(namespace, {})
        for ref_id in ref_ids:
            bucket.pop(ref_id, None)
        return len(ref_ids)

    def search(self, namespace: str, vector: np.ndarray, top_k: int, filters: dict | None = None) -> list[VectorHit]:
        bucket = self._data.get(namespace, {})
        query = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(query)) or 1.0
        scored: list[VectorHit] = []
        for ref_id, (vec, meta) in bucket.items():
            if filters and not all(meta.get(k) == v for k, v in filters.items()):
                continue
            scored.append(VectorHit(ref_id=ref_id, score=float(vec @ query / norm), meta=meta))
        scored.sort(key=lambda h: -h.score)
        return scored[:top_k]

    def count(self, namespace: str) -> int:
        return len(self._data.get(namespace, {}))


class ComponentRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: dict[str, Any] = {}
        self._state_path: Path = settings.data_dir / "components.json"
        self._overrides: dict[str, str] = self._load_overrides()

    # -------------------------------------------------------------- 状态持久化
    def _load_overrides(self) -> dict[str, str]:
        if self._state_path.exists():
            try:
                return dict(json.loads(self._state_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_overrides(self) -> None:
        with _STATE_LOCK:
            self._state_path.write_text(
                json.dumps(self._overrides, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def active_name(self, kind: str) -> str:
        default = {
            "llm": self.settings.llm_provider,
            "embedding": self.settings.embedding_provider,
            "vector_store": self.settings.vector_store,
            "object_store": self.settings.object_store,
            "web_search": self.settings.web_search_provider,
            "im_channel": self.settings.im_channel,
            "rerank": self.settings.rerank_provider,
        }.get(kind, "")
        return self._overrides.get(kind, default) or default

    def set_active(self, kind: str, name: str) -> None:
        with _STATE_LOCK:
            self._overrides[kind] = name
            self._cache.pop(kind, None)
            self._save_overrides()

    # ------------------------------------------------------------------ LLM
    def build_llm(self, name: str | None = None) -> LLMProvider:
        chosen = name or self.active_name("llm")
        s = self.settings
        if chosen in ("mock", "local", "") or not (s.llm_api_key or s.has_remote_llm):
            return MockLLMProvider()
        return OpenAICompatProvider(
            api_key=s.llm_api_key,
            base_url=s.llm_base_url,
            model=s.llm_model,
            temperature=s.llm_temperature,
            max_tokens=s.llm_max_tokens,
            timeout=s.llm_timeout_s,
        )

    def llm(self) -> LLMProvider:
        return self._cached("llm", self.build_llm)

    # ------------------------------------------------------------ Embedding
    def build_embedding(self, name: str | None = None) -> EmbeddingProvider:
        chosen = name or self.active_name("embedding")
        s = self.settings
        if chosen == "openai_compat" and s.embedding_api_key:
            return OpenAICompatEmbedder(
                api_key=s.embedding_api_key,
                base_url=s.embedding_base_url or s.llm_base_url,
                model=s.embedding_model,
                dim=s.embedding_dim,
            )
        return LocalHashEmbedder(dim=s.embedding_dim)

    def embedding(self) -> EmbeddingProvider:
        return self._cached("embedding", self.build_embedding)

    # ------------------------------------------------------------- VectorStore
    def build_vector_store(self, name: str | None = None) -> VectorStore:
        chosen = name or self.active_name("vector_store")
        s = self.settings
        if chosen == "pgvector":
            return PgVectorStore(s.database_url or "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/finonto")
        if chosen == "dashvector":
            return DashVectorStore(
                api_key=s.llm_api_key,
                endpoint=s.web_search_endpoint,
                collection="finonto_chunks",
                dim=s.embedding_dim,
            )
        if chosen == "memory":
            return InMemoryVectorStore()
        return SqliteNumpyVectorStore(f"sqlite:///{(s.data_dir / 'vectors.db').as_posix()}")

    def vector_store(self) -> VectorStore:
        return self._cached("vector_store", self.build_vector_store)

    # ------------------------------------------------------------ ObjectStore
    def build_object_store(self, name: str | None = None) -> ObjectStore:
        chosen = name or self.active_name("object_store")
        s = self.settings
        if chosen == "oss" and s.oss_bucket:
            return OSSObjectStore(
                bucket=s.oss_bucket,
                endpoint=s.oss_endpoint,
                access_key_id=s.oss_access_key_id,
                access_key_secret=s.oss_access_key_secret,
            )
        return LocalFSObjectStore(s.object_store_root)

    def object_store(self) -> ObjectStore:
        return self._cached("object_store", self.build_object_store)

    # -------------------------------------------------------------- DocParser
    def doc_parser(self, _fmt: str | None = None) -> DocParser:
        return self._cached("doc_parser", DefaultDocParser)

    # ------------------------------------------------------------- DataSource
    def data_source(self, uri: str) -> DataSource:
        if uri.startswith(("file://", "local://", "/")) or uri[1:3] == ":\\" or "\\" in uri:
            return FileDataSource()
        if uri.startswith("sqlite"):
            return SQLiteDataSource()
        if uri.startswith(("http://", "https://")):
            return HttpJsonDataSource()
        return JdbcDataSource()

    # -------------------------------------------------------------- WebSearch
    def build_web_search(self, name: str | None = None) -> WebSearch:
        chosen = name or self.active_name("web_search")
        s = self.settings
        if chosen == "duckduckgo":
            return DuckDuckGoSearch()
        if chosen in ("tavily", "bocha", "serper") and s.web_search_api_key:
            endpoint = s.web_search_endpoint or {
                "tavily": "https://api.tavily.com/search",
                "bocha": "https://api.bochaai.com/v1/web-search",
                "serper": "https://google.serper.dev/search",
            }[chosen]
            return ApiKeyWebSearch(chosen, endpoint, s.web_search_api_key, shape=chosen)
        return NoopWebSearch()

    def web_search(self) -> WebSearch:
        return self._cached("web_search", self.build_web_search)

    # ---------------------------------------------------------------- IM
    def build_im(self, name: str | None = None) -> IMChannel:
        chosen = name or self.active_name("im_channel")
        s = self.settings
        table = {"dingtalk": DingTalkChannel, "feishu": FeishuChannel, "wecom": WeComChannel, "slack": SlackChannel}
        if chosen in table and s.im_webhook_url:
            return table[chosen](s.im_webhook_url, s.im_secret)
        return ConsoleChannel()

    def im(self) -> IMChannel:
        return self._cached("im_channel", self.build_im)

    # ---------------------------------------------------------------- misc
    def _cached(self, key: str, factory):  # noqa: ANN001
        with _STATE_LOCK:
            if key not in self._cache or self._cache.get(f"{key}:name") != self.active_name(key):
                self._cache[key] = factory()
                self._cache[f"{key}:name"] = self.active_name(key)
            return self._cache[key]

    # ------------------------------------------------------------- 组件描述
    def describe(self) -> list[ProviderInfo]:
        s = self.settings
        out: list[ProviderInfo] = []

        out.append(
            ProviderInfo(
                kind="llm",
                name=self.active_name("llm"),
                display="抽取式本地基线（零依赖）",
                description="按任务分派的确定性推理实现，未配置密钥时的默认选项，可完整跑通问答/本体/Agent 链路。",
                configured=True,
                local_fallback=True,
                module="providers.llm.mock",
                capabilities=["chat", "json", "stream", "react"],
            )
        )
        for vendor in LLM_VENDORS:
            out.append(
                ProviderInfo(
                    kind="llm",
                    name=f"openai_compat:{vendor['key']}",
                    display=vendor["display"],
                    description=f"OpenAI 兼容 Chat Completions；base_url={vendor['base_url']}，默认 model={vendor['model']}。",
                    configured=(s.llm_provider != "mock" and s.llm_base_url.startswith(vendor["base_url"])),
                    config_hint={"FINONTO_LLM_BASE_URL": vendor["base_url"], "FINONTO_LLM_MODEL": vendor["model"], "FINONTO_LLM_API_KEY": "<密钥>"},
                    capabilities=["chat", "json", "stream"],
                    module="providers.llm.openai_compat",
                )
            )

        out.append(
            ProviderInfo(
                kind="embedding",
                name="local_hash",
                display="本地特征哈希 Embedding",
                description=f"带符号 n-gram 特征哈希 + 次线性 TF 加权，dim={s.embedding_dim}，确定性、可离线。",
                configured=self.active_name("embedding") == "local_hash",
                local_fallback=True,
                capabilities=["embed"],
                module="providers.embedding.local_hash",
            )
        )
        out.append(
            ProviderInfo(
                kind="embedding",
                name="openai_compat",
                display="OpenAI 兼容 Embeddings",
                description="支持 DashScope text-embedding-v3 / OpenAI text-embedding-3-small 等。",
                configured=bool(s.embedding_api_key),
                config_hint={"FINONTO_EMBEDDING_PROVIDER": "openai_compat", "FINONTO_EMBEDDING_API_KEY": "<密钥>", "FINONTO_EMBEDDING_MODEL": "text-embedding-v3"},
                capabilities=["embed"],
                module="providers.embedding.openai_compat",
            )
        )

        for name, display, desc, mods in (
            ("sqlite_numpy", "SQLite + numpy 向量库（默认）", "自带命名空间表，与业务表解耦；万级 chunk 全内存 cosine 检索。", True),
            ("pgvector", "PostgreSQL + pgvector", "生产级向量检索，需安装 pgvector 扩展并提供 database_url。", False),
            ("dashvector", "阿里云 DashVector", "托管向量库，需 dashvector 客户端与 API Key。", False),
            ("memory", "进程内向量库", "会话级临时检索，重启丢失，用于测试。", True),
        ):
            out.append(
                ProviderInfo(
                    kind="vector_store",
                    name=name,
                    display=display,
                    description=desc,
                    configured=self.active_name("vector_store") == name,
                    local_fallback=mods,
                    capabilities=["upsert", "search", "delete"],
                    module="providers.vectorstore",
                )
            )

        for name, display, desc, local in (
            ("local_fs", "本地文件系统对象存储（默认）", "写入 var/objects，键名做穿越防护。", True),
            ("oss", "阿里云 OSS / S3 兼容", "需 oss2 与 bucket 配置，用于生产环境原始文件留存。", False),
        ):
            out.append(
                ProviderInfo(
                    kind="object_store",
                    name=name,
                    display=display,
                    description=desc,
                    configured=self.active_name("object_store") == name,
                    local_fallback=local,
                    capabilities=["put", "get", "delete"],
                    module="providers.objectstore",
                )
            )

        parser = DefaultDocParser()
        out.append(
            ProviderInfo(
                kind="doc_parser",
                name=parser.name,
                display="多格式文档解析器",
                description="支持 " + "、".join(sorted(set(parser.formats))) + "；PDF/DOCX/XLSX 使用可选依赖，缺失时优雅降级为纯文本。",
                configured=True,
                capabilities=list(parser.formats),
                module="providers.docparser.parsers",
            )
        )

        for name, display, desc, hint in (
            ("file", "本地文件表数据源", "CSV / TSV / Excel / JSON 直接表格化，可反推对象类型属性。", "uri=local://data/products.csv"),
            ("sqlite", "SQLite 库表数据源", "读取行内库表结构与样本行。", "uri=sqlite:///D:/dw/mart.db"),
            ("http_json", "HTTP JSON 数据源", "GET 返回对象数组即可表格化，适合内部元数据服务。", "uri=https://meta.internal/tables"),
            ("jdbc", "JDBC 风格企业库", "提供 SQLAlchemy URL 即可接 MySQL/Oracle/PG 数仓。", "uri=mysql+pymysql://user:pw@host:3306/dw"),
        ):
            out.append(
                ProviderInfo(
                    kind="data_source",
                    name=name,
                    display=display,
                    description=desc,
                    configured=True,
                    local_fallback=name in ("file", "sqlite"),
                    config_hint={"example_uri": hint},
                    capabilities=["describe", "sample", "profile"],
                    module="providers.datasource.connectors",
                )
            )

        for name, display, desc, configured in (
            ("none", "不接入 IM", "所有通知只落本地 outbox。", True),
            ("dingtalk", "钉钉群机器人", "支持加签 webhook。", self.active_name("im_channel") == "dingtalk"),
            ("feishu", "飞书自定义机器人", "支持文本与卡片。", self.active_name("im_channel") == "feishu"),
            ("wecom", "企业微信群机器人", "markdown 消息。", self.active_name("im_channel") == "wecom"),
            ("slack", "Slack Incoming Webhook", "境外团队协作。", self.active_name("im_channel") == "slack"),
        ):
            out.append(
                ProviderInfo(
                    kind="im_channel",
                    name=name,
                    display=display,
                    description=desc,
                    configured=configured,
                    local_fallback=name == "none",
                    config_hint={"FINONTO_IM_CHANNEL": name, "FINONTO_IM_WEBHOOK_URL": "<webhook>", "FINONTO_IM_SECRET": "<加签密钥, 可选>"},
                    capabilities=["send"],
                    module="providers.im.channels",
                )
            )

        for name, display, desc, configured in (
            ("none", "关闭网络搜索", "Agent 的 search.web 返回空并说明原因。", True),
            ("duckduckgo", "DuckDuckGo（无需密钥）", "抓取 HTML 结果，适合外网可达环境。", self.active_name("web_search") == "duckduckgo"),
            ("tavily", "Tavily", "面向 LLM Agent 的搜索 API。", False),
            ("bocha", "博查 AI 搜索", "中文语料友好。", False),
            ("serper", "Serper (Google)", "低延迟 SERP。", False),
        ):
            out.append(
                ProviderInfo(
                    kind="web_search",
                    name=name,
                    display=display,
                    description=desc,
                    configured=configured,
                    local_fallback=name == "none",
                    config_hint={"FINONTO_WEB_SEARCH_PROVIDER": name, "FINONTO_WEB_SEARCH_API_KEY": "<密钥, 可选>"},
                    capabilities=["search", "fetch"],
                    module="providers.search.web",
                )
            )

        for name, display, desc in (
            ("heuristic", "启发式重排序（默认）", "词覆盖 / 短语精确命中 / 权威度 / 新近度 / 位置加权融合，零外部调用。"),
            ("llm", "LLM pointwise 重排序", "调用当前 LLM 对候选片段逐条打分并给理由。"),
            ("cross_encoder", "Cross-Encoder 重排序", "需部署 bge-reranker 之类的服务并配置 endpoint。"),
        ):
            out.append(
                ProviderInfo(
                    kind="rerank",
                    name=name,
                    display=display,
                    description=desc,
                    configured=self.active_name("rerank") == name,
                    local_fallback=name == "heuristic",
                    capabilities=["rerank"],
                    module="core.rag.reranker",
                )
            )

        for target in DEPLOY_TARGETS:
            out.append(
                ProviderInfo(
                    kind="deploy",
                    name=target["key"],
                    display=target["display"],
                    description=f"{target.get('description', '')} {target['hint']}".strip(),
                    configured=target["key"] == "local",
                    config_hint={"how": target["hint"]},
                    module="deployment",
                )
            )
        return out

    def health(self) -> dict[str, Any]:
        llm = self.llm()
        emb = self.embedding()
        vs = self.vector_store()
        return {
            "llm": {"active": self.active_name("llm"), "class": type(llm).__name__, "mock": isinstance(llm, MockLLMProvider)},
            "embedding": {"active": self.active_name("embedding"), "dim": emb.dim, "class": type(emb).__name__},
            "vector_store": {"active": self.active_name("vector_store"), "class": type(vs).__name__},
            "object_store": {"active": self.active_name("object_store")},
            "web_search": {"active": self.active_name("web_search")},
            "im_channel": {"active": self.active_name("im_channel")},
            "rerank": {"active": self.active_name("rerank")},
        }


registry = ComponentRegistry(get_settings())


def get_registry() -> ComponentRegistry:
    return registry


async def probe_llm(reg: ComponentRegistry | None = None) -> dict[str, Any]:
    """连通性探针：给组件设置页一个「测试连接」按钮的真实依据。"""
    reg = reg or registry
    provider = reg.build_llm()
    messages = [ChatMessage(role="user", content="[[TASK:generic]]\n只回复两个字：可用")]
    try:
        text, usage = await provider.complete(messages, task="generic", max_tokens=16)
        return {"ok": True, "provider": type(provider).__name__, "echo": text[:40], "usage": usage.total}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "provider": type(provider).__name__, "error": str(exc)[:300]}
