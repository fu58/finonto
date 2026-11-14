"""重排序组件：heuristic（默认，零依赖）/ llm（pointwise）/ cross_encoder（可选）。"""

from __future__ import annotations

import re
from typing import Any

from ..core.text import term_set, truncate
from .base import Message, RerankItem


class HeuristicReranker:
    """可解释的打分：查询词覆盖 + 短语命中 + 位置先验 + 权威度。

    企业里「为什么这条排第一」必须能回答，所以每一分都有出处（返回 meta.why）。
    """

    name = "heuristic"
    kind = "rerank"

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "provider": "builtin", "needs_key": False}

    def rerank(self, query: str, items: list[RerankItem], top_k: int = 8) -> list[RerankItem]:
        q_terms = term_set(query)
        if not q_terms:
            return items[:top_k]
        scored: list[RerankItem] = []
        for position, item in enumerate(items):
            text_terms = term_set(item.text)
            coverage = len(q_terms & text_terms) / len(q_terms)
            density = len(q_terms & text_terms) / max(1, len(text_terms)) ** 0.5
            phrase_bonus = 0.15 if query.strip() and query.strip() in item.text else 0.0
            position_bonus = 0.05 / (1 + position)
            authority = float(item.meta.get("authority", 3) or 3) / 5.0
            score = (
                coverage * 0.9
                + density * 0.4
                + phrase_bonus
                + position_bonus
                + authority * 0.1
            )
            scored.append(
                RerankItem(
                    chunk_id=item.chunk_id,
                    text=item.text,
                    score=round(score, 6),
                    meta={
                        **item.meta,
                        "why": f"覆盖 {coverage:.0%} · 密度 {density:.2f} · 权威 {authority:.1f}",
                    },
                )
            )
        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:top_k]


class LLMReranker:
    """pointwise：让模型给每个候选打 0-10 分，输出空格分隔的序号顺序。

    解析失败时退回 heuristic —— 重排失败不该让整个问答失败。
    """

    name = "llm"
    kind = "rerank"

    def __init__(self, llm, fallback: HeuristicReranker | None = None) -> None:
        self.llm = llm
        self.fallback = fallback or HeuristicReranker()

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "provider": "llm-assisted", "needs_key": True}

    def rerank(self, query: str, items: list[RerankItem], top_k: int = 8) -> list[RerankItem]:
        if not items:
            return []
        lines = [f"[c{i + 1}] {truncate(item.text, 400)}" for i, item in enumerate(items)]
        prompt = (
            "# 任务：rerank\n"
            f"【问题】\n{query}\n"
            f"【候选】\n" + "\n".join(lines) + "\n"
            "请只输出候选序号，按相关性从高到低，空格分隔。"
        )
        try:
            raw = self.llm.chat([Message(role="user", content=prompt)], max_tokens=120)
            order = [int(n) for n in re.findall(r"\d+", raw)]
        except Exception:  # noqa: BLE001
            return self.fallback.rerank(query, items, top_k)
        if not order:
            return self.fallback.rerank(query, items, top_k)
        result: list[RerankItem] = []
        seen: set[int] = set()
        for index in order:
            if 1 <= index <= len(items) and index not in seen:
                seen.add(index)
                item = items[index - 1]
                result.append(
                    RerankItem(
                        chunk_id=item.chunk_id,
                        text=item.text,
                        score=round(1.0 - len(result) * 0.05, 4),
                        meta={**item.meta, "why": "llm pointwise"},
                    )
                )
        for index, item in enumerate(items, start=1):
            if index not in seen:
                result.append(item)
        return result[:top_k]


class CrossEncoderReranker:
    """交给本机 cross-encoder（sentence-transformers）；不可用时退回 heuristic。"""

    name = "cross_encoder"
    kind = "rerank"

    def __init__(self, model_name: str = "BAAI/bge-reranker-base", fallback: HeuristicReranker | None = None) -> None:
        self.model_name = model_name
        self.fallback = fallback or HeuristicReranker()
        self._model = None
        self._unavailable_reason = ""

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import CrossEncoder  # type: ignore

            self._model = CrossEncoder(self.model_name)
        except Exception as exc:  # noqa: BLE001
            self._unavailable_reason = f"未安装 sentence-transformers 或模型不可用：{exc}"
            self._model = False
        return self._model

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": "local_model",
            "model": self.model_name,
            "needs_key": False,
            "available": self._load() is not False,
            "note": self._unavailable_reason,
        }

    def rerank(self, query: str, items: list[RerankItem], top_k: int = 8) -> list[RerankItem]:
        model = self._load()
        if not model or not items:
            return self.fallback.rerank(query, items, top_k)
        pairs = [(query, item.text) for item in items]
        scores = model.predict(pairs)
        ranked = sorted(
            (
                RerankItem(
                    chunk_id=item.chunk_id,
                    text=item.text,
                    score=float(score),
                    meta={**item.meta, "why": "cross-encoder"},
                )
                for item, score in zip(items, scores)
            ),
            key=lambda x: x.score,
            reverse=True,
        )
        return ranked[:top_k]
