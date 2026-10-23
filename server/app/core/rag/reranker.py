"""重排序：启发式（默认）/ LLM pointwise / Cross-Encoder 外服务。

启发式实现不依赖模型，但按查询意图做了特征工程（与具体领域无关）：
定义型问题偏好在含「是指/定义为」的句子所在块，数值型偏好含阈值与单位的块，
枚举型偏好含列表与分号的块，合规型偏好高权威度与文号/标准号。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ...providers.base import ChatMessage
from ...providers.registry import registry
from ...core.text import overlap, split_sentences, tokenize
from .query_understanding import UnderstoodQuery
from .retriever import Candidate

_NUM = re.compile(r"\d+(?:[.,]\d+)?\s*(?:%|万元|亿元|元|天|个月|年|倍|人|笔)")
_CITE = re.compile(r"(《[^》]{2,40}》|JR/T\s*\d+|GB/T\s*\d+|Basel|IFRS\s*\d+|CAS\s*\d+)")
_LISTY = re.compile(r"[；;]|(?:^|\n)\s*[-·（(]\s*\d")
_DEF_FORM = re.compile(r"(是指|指的是|定义为|是指经|称为|包括.{0,20}的)")


class HeuristicReranker:
    name = "heuristic"

    def rerank(
        self, understood: UnderstoodQuery, candidates: list[Candidate], top_k: int
    ) -> list[tuple[Candidate, float, str]]:
        query = understood.rewritten or understood.original
        weights = dict(understood.terms)
        intent = understood.intent
        results: list[tuple[Candidate, float, str]] = []
        for candidate in candidates:
            text = candidate.text
            features: dict[str, float] = {
                "coverage": _weighted_coverage(query, text, weights),
                "sentence": max((overlap(query, s) for s in split_sentences(text)), default=0.0),
                "phrase": 1.0 if any(e and e in text for e in understood.entities) else 0.0,
                "authority": float(candidate.meta.get("authority", 0.5) or 0.5),
                "length_fit": _length_fit(len(text)),
                "fusion_prior": min(candidate.fused_score * 40.0, 1.0),
                "intent": _intent_signal(intent, text)[0],
            }
            score = (
                0.30 * features["coverage"]
                + 0.20 * features["sentence"]
                + 0.12 * features["phrase"]
                + 0.10 * features["authority"]
                + 0.08 * features["length_fit"]
                + 0.10 * features["fusion_prior"]
                + 0.18 * features["intent"]
            ) * 1.99
            reason = _format_reason(features, intent)
            results.append((candidate, round(min(score, 1.99), 4), reason))
        results.sort(key=lambda item: -item[1])
        return results[:top_k]


def _intent_signal(intent: str, text: str) -> tuple[float, str]:
    if intent == "definition":
        return (1.0 if _DEF_FORM.search(text) else 0.0, "含定义句式")
    if intent == "quantitative":
        found = _NUM.findall(text)
        return (min(len(found) * 0.4, 1.0), f"含 {len(found)} 处数值阈值" if found else "无数值")
    if intent == "enumeration":
        return (1.0 if _LISTY.search(text) else 0.0, "含清单结构")
    if intent == "regulation":
        found = _CITE.findall(text)
        return (min(len(found) * 0.5, 1.0), "含监管文号" if found else "无文号")
    if intent == "relation":
        return (1.0 if re.search(r"(属于|包括|导致|触发|依赖|对应|区别于|之间)", text) else 0.0, "含关系表述")
    if intent == "procedure":
        return (min(len(re.findall(r"(第[一二三四五六七八九十]+步|[1-9][.)、])", text)) * 0.4, 1.0), "含步骤结构")
    if intent == "ontology_generation":
        return (1.0 if _DEF_FORM.search(text) or _CITE.search(text) else 0.3, "可作为概念抽取来源")
    return (0.5, "通用相关性")


def _weighted_coverage(query: str, text: str, weights: dict[str, float]) -> float:
    if not weights:
        return overlap(query, text)
    text_tokens = set(tokenize(text))
    total = sum(weights.values()) or 1.0
    hit = sum(w for token, w in weights.items() if any(t in text_tokens for t in tokenize(token)))
    return round(hit / total, 4)


def _length_fit(size: int) -> float:
    if size < 40:
        return 0.2
    if size < 120:
        return 0.7
    if size <= 900:
        return 1.0
    return 0.75


_INTENT_FEATURE_LABELS = {
    "definition": "含定义句式",
    "quantitative": "含数值阈值",
    "enumeration": "含清单结构",
    "regulation": "含监管文号",
    "relation": "含关系表述",
    "procedure": "含步骤结构",
}


def _format_reason(features: dict[str, float], intent: str) -> str:
    parts = [
        f"词覆盖 {features['coverage']:.2f}",
        f"最佳句匹配 {features['sentence']:.2f}",
    ]
    if features.get("phrase"):
        parts.append("实体精确命中")
    if features.get("intent", 0.0) >= 0.4:
        parts.append(_INTENT_FEATURE_LABELS.get(intent, "意图特征命中"))
    parts.append(f"权威度 {features['authority']:.2f}")
    return "、".join(parts[:5])


class LLMReranker:
    """pointwise：让模型对每个候选块与问题的相关性打 0-1 分。"""

    name = "llm"

    async def rerank_async(
        self, understood: UnderstoodQuery, candidates: list[Candidate], top_k: int
    ) -> list[tuple[Candidate, float, str]]:
        from ...providers.llm.mock import split_context  # 复用解析约定

        lines = [f"## 问题\n{understood.rewritten or understood.original}", "", "## 候选资料"]
        for i, candidate in enumerate(candidates, start=1):
            lines.append(f"[c{i}] {candidate.text[:600]}")
        prompt = "[[TASK:rerank]]\n请为每条候选资料与问题的相关性打分（0~1），只输出 JSON：{\"scores\":[{\"index\":0,\"score\":0.8,\"reason\":\"...\"}]}\n\n" + "\n".join(lines)
        provider = registry.llm()
        raw, _usage = await provider.complete(
            [ChatMessage(role="user", content=prompt)], task="rerank", json_mode=True
        )
        try:
            payload = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
            scores = payload.get("scores") or payload.get("ranked") or []
        except (AttributeError, json.JSONDecodeError):
            scores = []
        by_index: dict[int, tuple[float, str]] = {}
        for entry in scores:
            idx = int(entry.get("index", -1))
            by_index[idx] = (float(entry.get("score", 0.0) or 0.0), str(entry.get("reason", ""))[:120])
        results: list[tuple[Candidate, float, str]] = []
        for i, candidate in enumerate(candidates):
            score, reason = by_index.get(i, (candidate.fused_score * 50, "模型未打分，沿用融合分"))
            results.append((candidate, round(min(score, 1.0) * 1.99, 4), reason or "LLM 打分"))
        results.sort(key=lambda item: -item[1])
        return results[:top_k]

    def rerank(self, understood, candidates, top_k):  # noqa: ANN001
        raise NotImplementedError("LLMReranker 需要异步调用 rerank_async")


class CrossEncoderReranker:
    """外部 Cross-Encoder（TEI / bge-reranker）服务，需配置 endpoint。"""

    name = "cross_encoder"

    def __init__(self, endpoint: str, api_key: str = "") -> None:
        self.endpoint = endpoint
        self.api_key = api_key

    async def rerank_async(
        self, understood: UnderstoodQuery, candidates: list[Candidate], top_k: int
    ) -> list[tuple[Candidate, float, str]]:
        import httpx

        query = understood.rewritten or understood.original
        payload = {
            "query": query,
            "texts": [c.text[:1200] for c in candidates],
            "return_text": False,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self.endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        results: list[tuple[Candidate, float, str]] = []
        for entry in data.get("results") or []:
            idx = int(entry.get("index", 0))
            if idx < len(candidates):
                score = float(entry.get("score", 0.0))
                results.append((candidates[idx], round(score * 2, 4), f"cross-encoder {score:.3f}"))
        return results[:top_k]

    def rerank(self, understood, candidates, top_k):  # noqa: ANN001
        raise NotImplementedError("CrossEncoderReranker 需要异步调用 rerank_async")


async def rerank(
    understood: UnderstoodQuery,
    candidates: list[Candidate],
    top_k: int,
    *,
    provider: str | None = None,
) -> tuple[list[tuple[Candidate, float, str]], str]:
    """统一入口，返回 (排序结果, 使用的策略)。"""
    name = provider or registry.active_name("rerank")
    if not candidates:
        return [], name
    if name == "llm":
        return await LLMReranker().rerank_async(understood, candidates, top_k), name
    if name == "cross_encoder":
        endpoint = registry.settings.web_search_endpoint
        if not endpoint:
            understood.notes.append("cross_encoder 未配置 endpoint，退回启发式重排序")
            return HeuristicReranker().rerank(understood, candidates, top_k), "heuristic"
        return await CrossEncoderReranker(endpoint, registry.settings.web_search_api_key).rerank_async(
            understood, candidates, top_k
        ), name
    return HeuristicReranker().rerank(understood, candidates, top_k), "heuristic"
