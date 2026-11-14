"""组件协议（Protocol）——10 类可插拔组件的契约。

规则
----
1. 每个协议只描述「业务需要什么」，不描述厂商 SDK 长什么样；
2. 每个协议都有一个 **零密钥、功能完整** 的本地实现（不是占位）：
   `mock` LLM / `local_hash` 向量化 / `sqlite_numpy` 向量库 / `local_fs` 对象存储 / 内置解析器；
3. 新增厂商 = 新写一个类 + 在 `registry.py` 注册一行，业务代码零改动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Protocol, runtime_checkable


# --------------------------------------------------------------------------- #
# 对话 / 推理模型
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Message:
    role: str            # system / user / assistant
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def info(self) -> dict[str, Any]: ...

    def chat(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str: ...

    def chat_stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> Iterator[str]: ...


# --------------------------------------------------------------------------- #
# 向量化
# --------------------------------------------------------------------------- #
@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]: ...

    def info(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# 向量库
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class VectorHit:
    chunk_id: int
    score: float


@runtime_checkable
class VectorStore(Protocol):
    name: str

    def upsert(self, items: Iterable[tuple[int, list[float]]]) -> int: ...

    def delete(self, chunk_ids: Iterable[int]) -> int: ...

    def search(
        self,
        vector: list[float],
        top_k: int = 10,
        allowed_ids: set[int] | None = None,
    ) -> list[VectorHit]: ...

    def clear(self) -> int: ...

    def stats(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# 对象存储
# --------------------------------------------------------------------------- #
@runtime_checkable
class ObjectStore(Protocol):
    name: str

    def put(self, key: str, data: bytes, content_type: str = "") -> str: ...

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> bool: ...

    def exists(self, key: str) -> bool: ...

    def url_for(self, key: str) -> str: ...

    def info(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# 文档解析
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ParsedDocument:
    text: str
    pages: list[str] = field(default_factory=list)   # 每页/每 sheet 的文本，用于定位
    mime: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages) or 1


@runtime_checkable
class DocParser(Protocol):
    name: str
    supported_extensions: tuple[str, ...]

    def supports(self, filename: str) -> bool: ...

    def parse(self, filename: str, data: bytes) -> ParsedDocument: ...

    def info(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# 数据源
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class FetchedSource:
    uri: str
    filename: str
    mime: str
    data: bytes
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class DataSource(Protocol):
    name: str
    schemes: tuple[str, ...]

    def handles(self, uri: str) -> bool: ...

    def probe(self, uri: str) -> dict[str, Any]: ...

    def fetch(self, uri: str) -> FetchedSource: ...

    def info(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# 网络搜索
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""


@runtime_checkable
class WebSearch(Protocol):
    name: str

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]: ...

    def info(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# IM 通知
# --------------------------------------------------------------------------- #
@runtime_checkable
class IMChannel(Protocol):
    name: str

    def send(self, text: str, *, title: str = "") -> bool: ...

    def info(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# 重排序
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RerankItem:
    chunk_id: int
    text: str
    score: float
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Reranker(Protocol):
    name: str

    def rerank(self, query: str, items: list[RerankItem], top_k: int = 8) -> list[RerankItem]: ...

    def info(self) -> dict[str, Any]: ...


__all__ = [
    "Message",
    "LLMProvider",
    "EmbeddingProvider",
    "VectorHit",
    "VectorStore",
    "ObjectStore",
    "ParsedDocument",
    "DocParser",
    "FetchedSource",
    "DataSource",
    "SearchResult",
    "WebSearch",
    "IMChannel",
    "RerankItem",
    "Reranker",
]
