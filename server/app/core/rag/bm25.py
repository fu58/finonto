"""BM25（Okapi）自实现：中文靠 tokenizer 产出的单字 + 二元词。

与向量召回互补：BM25 擅长「条款编号、专有名词、精确短语」，
向量召回擅长「换个说法」。二者用 RRF 融合（见 retriever.py）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..text import tokenize


@dataclass(slots=True)
class BM25Doc:
    doc_id: int
    tokens: list[str]
    length: int = 0


@dataclass(slots=True)
class BM25Hit:
    doc_id: int
    score: float


@dataclass(slots=True)
class BM25Index:
    k1: float = 1.5
    b: float = 0.75
    docs: dict[int, BM25Doc] = field(default_factory=dict)
    _df: dict[str, int] = field(default_factory=dict)
    _avgdl: float = 0.0
    _total: int = 0

    # ------------------------------------------------------------------ #
    def add(self, doc_id: int, text: str) -> None:
        tokens = tokenize(text)
        self.docs[int(doc_id)] = BM25Doc(doc_id=int(doc_id), tokens=tokens, length=len(tokens))
        self._total += len(tokens)
        for term in set(tokens):
            self._df[term] = self._df.get(term, 0) + 1
        self._avgdl = self._total / max(1, len(self.docs))

    def build(self, rows: list[tuple[int, str]]) -> "BM25Index":
        self.docs.clear()
        self._df.clear()
        self._total = 0
        for doc_id, text in rows:
            self.add(doc_id, text)
        return self

    def __len__(self) -> int:
        return len(self.docs)

    # ------------------------------------------------------------------ #
    def _idf(self, term: str) -> float:
        n = len(self.docs)
        df = self._df.get(term, 0)
        if df == 0:
            return 0.0
        # 加 0.5 平滑，避免负值
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 20) -> list[BM25Hit]:
        query_terms = [t for t in tokenize(query) if t]
        if not query_terms or not self.docs:
            return []
        scores: dict[int, float] = {}
        for term in query_terms:
            idf = self._idf(term)
            if idf <= 0:
                continue
            for doc_id, doc in self.docs.items():
                freq = doc.tokens.count(term)
                if not freq:
                    continue
                denom = freq + self.k1 * (1 - self.b + self.b * doc.length / max(1e-6, self._avgdl))
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * freq * (self.k1 + 1) / denom
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        return [BM25Hit(doc_id=doc_id, score=round(score, 6)) for doc_id, score in ranked[:top_k]]
