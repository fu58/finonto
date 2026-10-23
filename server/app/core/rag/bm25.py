"""Okapi BM25 倒排索引（进程内，按命名空间缓存）。"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any

from ..text import tokenize


@dataclass
class BM25Hit:
    ref_id: int
    score: float
    meta: dict[str, Any] = field(default_factory=dict)


class BM25Index:
    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._postings: dict[str, dict[int, int]] = {}
        self._lengths: dict[int, int] = {}
        self._metas: dict[int, dict] = {}
        self._avg_len = 0.0
        self._lock = threading.RLock()

    def build(self, docs: list[tuple[int, str, dict]]) -> None:
        """docs: [(ref_id, text, meta)]，全量重建（万级规模在毫秒量级）。"""
        with self._lock:
            self._postings = {}
            self._lengths = {}
            self._metas = {}
            total = 0
            for ref_id, text, meta in docs:
                tokens = tokenize(text)
                if not tokens:
                    continue
                self._lengths[ref_id] = len(tokens)
                self._metas[ref_id] = meta or {}
                total += len(tokens)
                local: dict[str, int] = {}
                for token in tokens:
                    local[token] = local.get(token, 0) + 1
                for token, freq in local.items():
                    self._postings.setdefault(token, {})[ref_id] = freq
            self._avg_len = total / max(len(self._lengths), 1)

    @property
    def size(self) -> int:
        return len(self._lengths)

    def idf(self, token: str) -> float:
        n = len(self._lengths) or 1
        df = len(self._postings.get(token, {}))
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def idf_map(self) -> dict[str, float]:
        return {token: self.idf(token) for token in self._postings}

    def search(
        self, query: str, top_k: int = 40, filters: dict[str, Any] | None = None
    ) -> list[BM25Hit]:
        tokens = tokenize(query)
        if not tokens or not self._lengths:
            return []
        scores: dict[int, float] = {}
        for token in tokens:
            postings = self._postings.get(token)
            if not postings:
                continue
            weight = self.idf(token)
            for ref_id, freq in postings.items():
                length = self._lengths.get(ref_id, 1)
                denom = freq + self.k1 * (1 - self.b + self.b * length / (self._avg_len or 1))
                scores[ref_id] = scores.get(ref_id, 0.0) + weight * (freq * (self.k1 + 1)) / denom
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        out: list[BM25Hit] = []
        for ref_id, score in ranked:
            meta = self._metas.get(ref_id, {})
            if filters and not _match(meta, filters):
                continue
            out.append(BM25Hit(ref_id=ref_id, score=float(score), meta=meta))
            if len(out) >= top_k:
                break
        return out


def _match(meta: dict, filters: dict) -> bool:
    for key, expected in filters.items():
        value = meta.get(key)
        if isinstance(expected, (list, tuple, set)):
            if value not in expected:
                return False
        elif expected is not None and value != expected:
            return False
    return True


class IndexRegistry:
    """按命名空间维护 BM25 索引，供检索层直接调用。"""

    def __init__(self) -> None:
        self._indexes: dict[str, BM25Index] = {}
        self._versions: dict[str, int] = {}
        self._lock = threading.RLock()

    def get(self, namespace: str) -> BM25Index:
        with self._lock:
            return self._indexes.setdefault(namespace, BM25Index())

    def rebuild(self, namespace: str, docs: list[tuple[int, str, dict]]) -> int:
        index = self.get(namespace)
        index.build(docs)
        return index.size

    def invalidate(self, namespace: str | None = None) -> None:
        with self._lock:
            if namespace is None:
                self._indexes.clear()
            else:
                self._indexes.pop(namespace, None)

    def touch_version(self, namespace: str) -> int:
        with self._lock:
            self._versions[namespace] = self._versions.get(namespace, 0) + 1
            return self._versions[namespace]


index_registry = IndexRegistry()
