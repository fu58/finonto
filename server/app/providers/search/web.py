"""网络搜索适配：默认关闭 / DuckDuckGo(无需 key) / Tavily / Bocha / Serper。

Agent 的 `search.web` 工具与 RAG 的联网补充召回共用此接口；未配置时返回明确的
不可用原因，而不是静默失败。
"""

from __future__ import annotations

import re
import urllib.parse

import httpx

from ..base import ProviderError, WebResult


class NoopWebSearch:
    name = "none"

    def search(self, query: str, top_k: int = 5) -> list[WebResult]:
        return []

    def fetch(self, url: str, max_chars: int = 8000) -> str:
        raise ProviderError("未配置网络搜索组件（设置 FINONTO_WEB_SEARCH_PROVIDER=duckduckgo|tavily|bocha|serper）")


class DuckDuckGoSearch:
    name = "duckduckgo"

    ENDPOINTS = ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/")

    def search(self, query: str, top_k: int = 5) -> list[WebResult]:
        results: list[WebResult] = []
        for endpoint in self.ENDPOINTS:
            try:
                resp = httpx.post(
                    endpoint,
                    data={"q": query},
                    headers={"User-Agent": "Mozilla/5.0 (FinOnto research agent)"},
                    timeout=15.0,
                    follow_redirects=True,
                )
                resp.raise_for_status()
            except httpx.HTTPError:
                continue
            html = resp.text
            for block in re.findall(
                r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
                r'(?:<a[^>]*class="result__snippet"[^>]*>(.*?)</a>)?',
                html,
                re.S,
            ):
                url, title, snippet = block
                url = _ddg_unwrap(urllib.parse.unquote(url or ""))
                if not url.startswith("http"):
                    continue
                results.append(
                    WebResult(
                        title=_strip(title),
                        url=url,
                        snippet=_strip(snippet)[:400],
                        source="duckduckgo",
                    )
                )
                if len(results) >= top_k:
                    return results
            if results:
                break
        return results

    def fetch(self, url: str, max_chars: int = 8000) -> str:
        try:
            resp = httpx.get(url, timeout=20.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"抓取失败: {exc}") from exc
        ctype = resp.headers.get("content-type", "")
        text = resp.text if "html" in ctype else resp.text
        if "html" in ctype:
            text = _html_to_text_local(text)
        return text[:max_chars]


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html or "")).strip()


def _html_to_text_local(html: str) -> str:
    body = re.search(r"<body[^>]*>(.*)</body>", html or "", re.S | re.I)
    text = _strip(body.group(1) if body else (html or ""))
    return re.sub(r"\s{3,}", "\n\n", text)


def _ddg_unwrap(url: str) -> str:
    if "uddg=" in url:
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return (parsed.get("uddg") or [url])[0]
    return url


class ApiKeyWebSearch:
    """Tavily / Bocha / Serper 这类返回 JSON 的搜索 API。"""

    def __init__(self, name: str, endpoint: str, api_key: str, *, shape: str = "tavily") -> None:
        self.name = name
        self.endpoint = endpoint
        self.api_key = api_key
        self.shape = shape
        if not api_key:
            raise ProviderError(f"{name} 需要配置 FINONTO_WEB_SEARCH_API_KEY")

    def search(self, query: str, top_k: int = 5) -> list[WebResult]:
        payload: dict = {"query": query, "max_results": top_k}
        headers = {"Content-Type": "application/json"}
        if self.shape == "serper":
            payload = {"q": query, "num": top_k}
            headers["X-API-KEY"] = self.api_key
        elif self.shape == "bocha":
            payload = {"query": query, "count": top_k}
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            payload.update({"api_key": self.api_key, "search_depth": "advanced"})
        try:
            resp = httpx.post(self.endpoint, json=payload, headers=headers, timeout=25.0)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"{self.name} 搜索失败: {exc}") from exc
        items = data.get("results") or data.get("webPages", {}).get("value") or data.get("data", {}).get("web", [])
        out: list[WebResult] = []
        for item in items[:top_k]:
            out.append(
                WebResult(
                    title=_strip(str(item.get("title", ""))),
                    url=str(item.get("url") or item.get("link") or ""),
                    snippet=_strip(str(item.get("content") or item.get("snippet") or item.get("summary") or ""))[:500],
                    source=self.name,
                )
            )
        return out

    def fetch(self, url: str, max_chars: int = 8000) -> str:
        return DuckDuckGoSearch().fetch(url, max_chars)
