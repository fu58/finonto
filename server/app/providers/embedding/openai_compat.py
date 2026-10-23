"""OpenAI 兼容 / DashScope Embeddings 适配。"""

from __future__ import annotations

import numpy as np

from ..base import ProviderError
import httpx

_BATCH_LIMIT = 10


class OpenAICompatEmbedder:
    name = "openai_compat_embedding"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        dim: int = 1024,
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self.timeout = timeout

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        url = f"{self.base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        rows: list[list[float]] = []
        try:
            with httpx.Client(timeout=self.timeout) as client:
                for start in range(0, len(texts), _BATCH_LIMIT):
                    batch = [t if t.strip() else " " for t in texts[start : start + _BATCH_LIMIT]]
                    resp = client.post(url, headers=headers, json={"model": self.model, "input": batch})
                    resp.raise_for_status()
                    data = resp.json()["data"]
                    rows.extend(item["embedding"] for item in sorted(data, key=lambda d: d.get("index", 0)))
        except (httpx.HTTPError, KeyError) as exc:
            raise ProviderError(f"Embedding 调用失败: {exc}") from exc
        arr = np.asarray(rows, dtype=np.float32)
        self.dim = arr.shape[1]
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms
