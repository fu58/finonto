"""OpenAI 兼容 Chat Completions 适配层。

覆盖绝大多数模型厂商（OpenAI / DashScope 兼容模式 / DeepSeek / Moonshot /
智谱 / 自建 vLLM / Ollama OpenAI 端点），只需换 `base_url` + `model` + `api_key`。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..base import ChatMessage, LLMProvider, LLMUsage, ProviderError


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        timeout: float = 120.0,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.extra_body = extra_body or {}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _payload(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None,
        max_tokens: int | None,
        json_mode: bool,
        stream: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": stream,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(self.extra_body)
        return body

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        task: str = "generic",
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> tuple[str, LLMUsage]:
        url = f"{self.base_url}/chat/completions"
        payload = self._payload(
            messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode, stream=False
        )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(url, headers=self._headers(), json=payload)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise ProviderError(f"LLM 调用失败: {exc}") from exc
            data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        usage = data.get("usage") or {}
        return text, LLMUsage(
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        task: str = "generic",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        url = f"{self.base_url}/chat/completions"
        payload = self._payload(
            messages, temperature=temperature, max_tokens=max_tokens, json_mode=False, stream=True
        )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                async with client.stream("POST", url, headers=self._headers(), json=payload) as resp:
                    resp.raise_for_status()
                    async for raw in resp.aiter_lines():
                        if not raw or not raw.startswith("data:"):
                            continue
                        chunk = raw[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            obj = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        delta = ((obj.get("choices") or [{}])[0].get("delta") or {})
                        piece = delta.get("content")
                        if piece:
                            yield piece
            except httpx.HTTPError as exc:
                raise ProviderError(f"LLM 流式调用失败: {exc}") from exc
