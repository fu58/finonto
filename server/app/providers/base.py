"""组件（Provider）适配层协议定义。

平台把「模型厂商 / Embedding / 向量数据库 / 对象存储 / 文档格式 / 数据源 / IM 渠道 /
网络搜索」都抽象成 Provider，运行时按配置选择实现；任何一项未配置时都能退回到
本地零依赖实现，因此主流程无需外部服务即可跑通。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Protocol, runtime_checkable

import numpy as np

ProviderKind = Literal[
    "llm",
    "embedding",
    "vector_store",
    "object_store",
    "doc_parser",
    "data_source",
    "im_channel",
    "web_search",
    "rerank",
    "deploy",
]


@dataclass
class ProviderInfo:
    kind: ProviderKind
    name: str
    display: str
    description: str
    configured: bool = False
    local_fallback: bool = False
    config_hint: dict[str, str] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    module: str = ""


class ProviderError(RuntimeError):
    pass


# --------------------------------------------------------------------------- LLM


@dataclass
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        task: str = "generic",
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> tuple[str, LLMUsage]: ...

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        task: str = "generic",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]: ...


# ---------------------------------------------------------------------- Embedding
@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...


# --------------------------------------------------------------------- VectorDB
@dataclass
class VectorHit:
    ref_id: int
    score: float
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class VectorStore(Protocol):
    name: str

    def upsert(self, namespace: str, vectors: dict[int, np.ndarray], meta: dict[int, dict] | None = None) -> int: ...

    def delete(self, namespace: str, ref_ids: list[int]) -> int: ...

    def search(
        self, namespace: str, vector: np.ndarray, top_k: int, filters: dict[str, Any] | None = None
    ) -> list[VectorHit]: ...

    def count(self, namespace: str) -> int: ...


# ------------------------------------------------------------------- ObjectStore
@runtime_checkable
class ObjectStore(Protocol):
    name: str

    def put(self, key: str, data: bytes, content_type: str = "") -> str: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> bool: ...

    def path_for(self, key: str) -> Path | None: ...


# --------------------------------------------------------------------- DocParser
@dataclass
class ParsedBlock:
    text: str
    locator: str = ""
    page: int | None = None
    heading: str = ""
    kind: str = "paragraph"
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDocument:
    blocks: list[ParsedBlock]
    doc_format: str
    title: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class DocParser(Protocol):
    name: str
    formats: tuple[str, ...]

    def can_parse(self, doc_format: str) -> bool: ...

    def parse(self, payload: bytes, doc_format: str, filename: str = "") -> ParsedDocument: ...


# --------------------------------------------------------------------- DataSource
@dataclass
class SourceTable:
    name: str
    columns: list[str]
    rows: list[list[Any]]
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class DataSource(Protocol):
    name: str

    def describe(self, uri: str) -> list[SourceTable]: ...

    def sample(self, uri: str, table: str, limit: int = 20) -> SourceTable: ...


# ----------------------------------------------------------------------- IM
@runtime_checkable
class IMChannel(Protocol):
    name: str

    def send(self, target: str, text: str, *, title: str = "") -> dict[str, Any]: ...


# ------------------------------------------------------------------- WebSearch
@dataclass
class WebResult:
    title: str
    url: str
    snippet: str
    source: str = "web"


@runtime_checkable
class WebSearch(Protocol):
    name: str

    def search(self, query: str, top_k: int = 5) -> list[WebResult]: ...

    def fetch(self, url: str, max_chars: int = 8000) -> str: ...


# ----------------------------------------------------------------------- Rerank
@runtime_checkable
class RerankProvider(Protocol):
    name: str

    def rerank(
        self, query: str, candidates: list[dict[str, Any]], top_k: int
    ) -> list[tuple[int, float, str]]:
        """返回 (candidate_index, score, reason)。"""
        ...
