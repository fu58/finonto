"""引用绑定：把答案里的 ``[cN]`` 变回「文档名 + 定位 + 原文引句」。

硬规则：**没被答案引用的片段不会出现在引用列表里**。
这样前端展示的每一条来源都一定是模型真的用过的，而不是「检索到但没看」的候选。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .retriever import RetrievedChunk

TAG_RE = re.compile(r"[\[【](c\d+)[\]】]")


@dataclass(slots=True)
class Citation:
    tag: str
    chunk_id: int
    document_id: int
    doc_title: str
    locator: str
    quote: str
    score: float = 0.0
    signals: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "tag": self.tag,
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "doc_title": self.doc_title,
            "locator": self.locator,
            "quote": self.quote,
            "score": round(self.score, 4),
        }


def build_context(chunks: list[RetrievedChunk], *, max_chars: int = 480) -> tuple[str, dict[str, RetrievedChunk]]:
    """拼出带 ``[cN]`` 标记的上下文；返回 (上下文文本, tag→片段)。

    每块头部写「文档名｜定位｜文种｜权威度」，让模型能决定该信哪一条。
    """
    lines: list[str] = []
    tag_map: dict[str, RetrievedChunk] = {}
    for index, chunk in enumerate(chunks, start=1):
        tag = f"c{index}"
        tag_map[tag] = chunk
        head = (
            f"[{tag}] {chunk.doc_title}｜{chunk.locator}"
            f"｜{chunk.signals.get('doc_type', '')}"
            f"｜权威度{chunk.signals.get('authority', 3)}"
        )
        body = chunk.text.strip()
        if len(body) > max_chars:
            body = body[:max_chars] + "…"
        lines.append(f"{head}\n{body}")
    return "\n\n".join(lines), tag_map


def bind(answer: str, tag_map: dict[str, RetrievedChunk]) -> list[Citation]:
    """按出现顺序把 [cN] 绑定成引用；未出现的丢弃，重复的合并。"""
    seen: set[str] = set()
    citations: list[Citation] = []
    for match in TAG_RE.finditer(answer or ""):
        tag = match.group(1)
        if tag in seen or tag not in tag_map:
            continue
        seen.add(tag)
        chunk = tag_map[tag]
        quote = chunk.text.strip().replace("\n", " ")
        if len(quote) > 220:
            quote = quote[:220] + "…"
        citations.append(
            Citation(
                tag=f"[{tag}]",
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                doc_title=chunk.doc_title,
                locator=chunk.locator,
                quote=quote,
                score=chunk.score,
                signals=chunk.signals,
            )
        )
    return citations


def uncited_tags(answer: str, tag_map: dict[str, RetrievedChunk]) -> list[str]:
    used = {match.group(1) for match in TAG_RE.finditer(answer or "")}
    return [tag for tag in tag_map if tag not in used]


def normalize_tags(answer: str) -> str:
    """把 ``【c1】`` 统一成 ``[c1]``，避免前端解析两套标记。"""
    return re.sub(r"【(c\d+)】", r"[\1]", answer or "")
