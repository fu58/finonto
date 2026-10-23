"""本地 Embedding：带符号特征哈希 + 次线性 TF 加权。

零依赖、确定性、可离线复现；对中英混合文本的 cosine 相似度近似 TF-IDF 余弦，
足以支撑 BM25 之外的稠密召回通道。接真实 Embedding 模型时换成 OpenAICompatEmbedder。
"""

from __future__ import annotations

import hashlib
import math

import numpy as np

from ...core.text import tokenize


def _hash_bucket(token: str, dim: int) -> tuple[int, float]:
    digest = hashlib.md5(token.encode("utf-8")).digest()
    idx = int.from_bytes(digest[:4], "big") % dim
    sign = 1.0 if digest[4] & 1 else -1.0
    return idx, sign


class LocalHashEmbedder:
    name = "local_hash"

    def __init__(self, dim: int = 384) -> None:
        self.dim = int(dim)
        self._idf: dict[str, float] = {}
        self._doc_count = 0

    def fit(self, texts: list[str]) -> None:
        """用一个语料估计 IDF，提升稠密通道的区分度。"""
        df: dict[str, int] = {}
        for text in texts:
            for tok in set(tokenize(text)):
                df[tok] = df.get(tok, 0) + 1
        total = max(len(texts), 1)
        self._idf = {tok: math.log(1 + total / (c or 1)) for tok, c in df.items()}
        self._doc_count = total

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            counts: dict[str, int] = {}
            for tok in tokenize(text):
                counts[tok] = counts.get(tok, 0) + 1
            for tok, c in counts.items():
                idx, sign = _hash_bucket(tok, self.dim)
                weight = (1.0 + math.log(c)) * self._idf.get(tok, 1.0)
                out[row, idx] += sign * weight
            norm = float(np.linalg.norm(out[row]))
            if norm:
                out[row] /= norm
        return out

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]
