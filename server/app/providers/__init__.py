"""可插拔组件适配层：10 类组件的协议、实现与注册表。"""

from .base import (
    EmbeddingProvider,
    LLMProvider,
    Message,
    ObjectStore,
    ParsedDocument,
    RerankItem,
    SearchResult,
    VectorHit,
    VectorStore,
)
from .registry import (
    CATALOG,
    CATEGORY_LABELS,
    Components,
    activate,
    build_components,
    catalog,
    get_components,
    probe,
    reset_components,
)

__all__ = [
    "Message",
    "LLMProvider",
    "EmbeddingProvider",
    "VectorStore",
    "VectorHit",
    "ObjectStore",
    "ParsedDocument",
    "SearchResult",
    "RerankItem",
    "Components",
    "CATALOG",
    "CATEGORY_LABELS",
    "build_components",
    "get_components",
    "reset_components",
    "catalog",
    "activate",
    "probe",
]
