"""网络搜索组件：none（默认关闭）/ duckduckgo（免密钥）/ tavily / bocha / serper。"""

from __future__ import annotations

import html
import re
from typing import Any

from .base import SearchResult


class NoWebSearch:
    """默认关闭：企业内网常见要求；Agent 的 search.* 工具会明确返回「已关闭」。"""

    name = "none"
    kind = "web_search"

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "provider": "none", "needs_key": False, "enabled": False}

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        return []


class DuckDuckGoSearch:
    """免密钥：抓公开 HTML 结果页。仅用于演示与内部查证，不保证稳定性。"""

    name = "duckduckgo"
    kind = "web_search"
    endpoint = "https://html.duckduckgo.com/html/"

    def __init__(self, timeout: float = 12.0) -> None:
        self.timeout = timeout

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "provider": "duckduckgo", "needs_key": False, "enabled": True}

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        import httpx

        headers = {"User-Agent": "Mozilla/5.0 (compatible; FinOnto/0.1)"}
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            response = client.post(self.endpoint, data={"q": query}, headers=headers)
            response.raise_for_status()
        results: list[SearchResult] = []
        for block in re.findall(r'<a rel="nofollow" class="result__a".*?</a>', response.text, re.S):
            href = re.search(r'href="([^"]+)"', block)
            title = re.sub(r"<[^>]+>", "", block)
            if href:
                results.append(
                    SearchResult(
                        title=html.unescape(title).strip(),
                        url=html.unescape(href.group(1)),
                        source=self.name,
                    )
                )
            if len(results) >= top_k:
                break
        return results


class TavilySearch:
    name = "tavily"
    kind = "web_search"

    def __init__(self, api_key: str, timeout: float = 20.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": "tavily",
            "needs_key": True,
            "key_configured": bool(self.api_key),
        }

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        if not self.api_key:
            return []
        import httpx

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": self.api_key,
                    "query": query,
                    "max_results": top_k,
                    "search_depth": "basic",
                },
            )
            response.raise_for_status()
            data = response.json()
        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
                source=self.name,
            )
            for item in data.get("results", [])[:top_k]
        ]


class BochaSearch:
    name = "bocha"
    kind = "web_search"

    def __init__(self, api_key: str, timeout: float = 20.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": "bocha",
            "needs_key": True,
            "key_configured": bool(self.api_key),
        }

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        if not self.api_key:
            return []
        import httpx

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                "https://api.bochaai.com/v1/web-search",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"query": query, "count": top_k, "summary": True},
            )
            response.raise_for_status()
            data = response.json()
        pages = data.get("data", {}).get("webPages", {}).get("value", [])
        return [
            SearchResult(
                title=item.get("name", ""),
                url=item.get("url", ""),
                snippet=item.get("snippet", ""),
                source=self.name,
            )
            for item in pages[:top_k]
        ]


class SerperSearch:
    name = "serper"
    kind = "web_search"

    def __init__(self, api_key: str, timeout: float = 20.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": "serper",
            "needs_key": True,
            "key_configured": bool(self.api_key),
        }

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        if not self.api_key:
            return []
        import httpx

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                json={"q": query, "num": top_k},
            )
            response.raise_for_status()
            data = response.json()
        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                source=self.name,
            )
            for item in data.get("organic", [])[:top_k]
        ]
