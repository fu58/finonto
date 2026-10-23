"""文档切块策略：按块 -> 段落 -> 句子累积，控制 token 预算与重叠。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..text import split_sentences

_CJK = re.compile(r"[一-鿿]")


def estimate_tokens(text: str) -> int:
    """粗略 token 估计：中文按字、其他按 4 字符 1 token。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return cjk + max(1, (len(text) - cjk) // 4)


@dataclass
class ChunkDraft:
    text: str
    locator: str
    tokens: int
    heading: str = ""
    page: int | None = None
    meta: dict | None = None


def chunk_blocks(
    blocks,
    *,
    max_tokens: int = 420,
    overlap_tokens: int = 60,
    min_tokens: int = 40,
) -> list[ChunkDraft]:
    """把 ParsedBlock 序列聚合成带定位信息的 chunk；标题参与上下文但不单独成片。"""
    chunks: list[ChunkDraft] = []
    buffer: list[str] = []
    buffer_tokens = 0
    locators: list[str] = []
    headings: list[str] = []
    pages: list[int] = []

    def push() -> None:
        nonlocal buffer, buffer_tokens, locators, headings, pages
        if not buffer:
            return
        text = "\n".join(buffer).strip()
        if text:
            chunks.append(
                ChunkDraft(
                    text=text,
                    locator="~".join([loc for loc in locators if loc][:3]) or f"chunk-{len(chunks)}",
                    tokens=estimate_tokens(text),
                    heading=headings[-1] if headings else "",
                    page=pages[0] if pages else None,
                    meta={"heading": headings[-1] if headings else "", "locators": locators[:6]},
                )
            )
        if overlap_tokens and buffer_tokens > overlap_tokens:
            tail = _tail_by_tokens(text, overlap_tokens)
            buffer = [tail] if tail else []
            buffer_tokens = estimate_tokens(tail)
        else:
            buffer, buffer_tokens = [], 0
        locators, headings, pages = [], [], []

    for block in blocks:
        for sentence in split_sentences(block.text) or [block.text]:
            sentence_tokens = estimate_tokens(sentence)
            if sentence_tokens > max_tokens:
                push()
                for piece in _split_long(sentence, max_tokens):
                    chunks.append(
                        ChunkDraft(
                            text=piece,
                            locator=block.locator,
                            tokens=estimate_tokens(piece),
                            heading=block.heading,
                            page=block.page,
                        )
                    )
                continue
            if buffer_tokens + sentence_tokens > max_tokens and buffer:
                push()
            buffer.append(sentence)
            buffer_tokens += sentence_tokens
            if block.locator and block.locator not in locators:
                locators.append(block.locator)
            if getattr(block, "heading", ""):
                headings.append(block.heading)
            if getattr(block, "page", None):
                pages.append(block.page)
    push()
    if min_tokens and chunks:
        chunks = _merge_undersized(chunks, min_tokens=min_tokens, max_tokens=max_tokens)
    return chunks


def _tail_by_tokens(text: str, tokens: int) -> str:
    sentences = split_sentences(text)
    tail: list[str] = []
    total = 0
    for sentence in reversed(sentences):
        t = estimate_tokens(sentence)
        if total + t > tokens:
            break
        tail.insert(0, sentence)
        total += t
    return " ".join(tail).strip()


def _split_long(sentence: str, max_tokens: int) -> list[str]:
    limit = max(20, max_tokens)
    window = limit * 2
    return [sentence[i : i + window] for i in range(0, len(sentence), window)] or [sentence]


def _merge_undersized(chunks: list[ChunkDraft], *, min_tokens: int, max_tokens: int) -> list[ChunkDraft]:
    merged: list[ChunkDraft] = []
    for chunk in chunks:
        if merged and chunk.tokens < min_tokens and merged[-1].tokens + chunk.tokens <= max_tokens:
            prev = merged[-1]
            prev.text = f"{prev.text}\n{chunk.text}".strip()
            prev.tokens = estimate_tokens(prev.text)
            prev.locator = f"{prev.locator}~{chunk.locator}"[:60]
            continue
        merged.append(chunk)
    return merged
