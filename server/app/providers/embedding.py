"""向量化组件：本地特征哈希（零密钥）+ 任意 OpenAI 兼容 embeddings。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np

from ..core.text import normalize, tokenize


class LocalHashEmbedding:
    """特征哈希（hashing trick），384 维，纯 numpy，确定性。

    做法：把 token（含二元词）用 blake2b 哈希到 ``dim`` 个桶之一，按符号累加，再做 L2 归一化。
    中文单字/二元词都进桶，所以「维保」和「维护保养」会有可观的重叠——这正是零密钥路径
    仍能做出「语义近邻」效果的原因（不追求 SOTA，追求可解释 + 可离线）。
    """

    name = "local_hash"
    kind = "embedding"

    def __init__(self, dim: int = 384) -> None:
        self.dim = max(64, int(dim))

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": "local_hash",
            "dim": self.dim,
            "needs_key": False,
            "note": "特征哈希；换真实 embedding 后需要逐篇重建索引",
        }

    # ------------------------------------------------------------------ #
    def _bucket(self, token: str) -> tuple[int, float]:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest[:8], "little")
        index = value % self.dim
        sign = 1.0 if (value >> 63) & 1 == 0 else -1.0
        return index, sign

    def embed_one(self, text: str) -> list[float]:
        vector = np.zeros(self.dim, dtype=np.float32)
        tokens = tokenize(text)
        if not tokens:
            return vector.tolist()
        for token in tokens:
            index, sign = self._bucket(token)
            weight = 1.0 + 0.5 * min(len(token), 4) / 4.0
            vector[index] += sign * weight
        norm = float(np.linalg.norm(vector))
        if norm > 0:
            vector /= norm
        return vector.tolist()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(text) for text in texts]


class OpenAICompatEmbedding:
    """任意 OpenAI 兼容 ``/embeddings`` 接口（百炼、OpenAI、bge-m3 自建等）。"""

    name = "openai_compat"
    kind = "embedding"

    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "text-embedding-3-small",
        dim: int = 1024,
        timeout: float = 60.0,
        batch_size: int = 32,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = int(dim)
        self.timeout = timeout
        self.batch_size = batch_size

    def info(self) -> dict[str, Any]:
        return {
            "name": f"openai_compat:{self.model}",
            "provider": "openai_compat",
            "dim": self.dim,
            "needs_key": True,
            "key_configured": bool(self.api_key),
            "base_url": self.base_url,
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        cleaned = [normalize(t)[:6000] for t in texts]
        out: list[list[float]] = []
        with httpx.Client(timeout=self.timeout) as client:
            for start in range(0, len(cleaned), self.batch_size):
                batch = cleaned[start : start + self.batch_size]
                response = client.post(
                    f"{self.base_url}/embeddings",
                    headers=self._headers(),
                    content=json.dumps({"model": self.model, "input": batch}),
                )
                response.raise_for_status()
                data = response.json()["data"]
                data.sort(key=lambda item: item.get("index", 0))
                for item in data:
                    vector = item["embedding"]
                    if len(vector) != self.dim:
                        self.dim = len(vector)
                    out.append(vector)
        return out

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
