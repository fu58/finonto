"""文档切块：按段落聚合 + 句子级重叠，保留可读的定位信息。

切块质量直接决定 RAG 上限，所以这里做三件事而不是简单按长度截断：
1. **标题感知**：识别 Markdown 标题与「第X章 / 第X条」，给出人类可读的 locator；
2. **句子对齐**：只在句子边界切，避免把一句话切成两半；
3. **重叠回溯**：相邻块保留最后若干句子作重叠，缓解「答案被切断」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..text import normalize, split_sentences

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
CLAUSE_RE = re.compile(r"^第[一二三四五六七八九十百零\d]+[章节条款]")
SENTENCE_JOIN = ""


@dataclass(slots=True)
class Chunk:
    ordinal: int
    text: str
    locator: str
    token_count: int


def _estimate_tokens(text: str) -> int:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return cjk + max(0, len(text) - cjk) // 4 + 1


def _split_blocks(text: str) -> list[tuple[str, str]]:
    """返回 [(section_title, block_text)]。"""
    blocks: list[tuple[str, str]] = []
    section = ""
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            joined = normalize("\n".join(buffer))
            if joined:
                blocks.append((section, joined))
            buffer.clear()

    for raw_line in normalize(text).splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        heading = HEADING_RE.match(line)
        if heading:
            flush()
            section = heading.group(2).strip()
            continue
        if CLAUSE_RE.match(line):
            # 「第X条」自成一段，便于条款级引用
            flush()
            buffer.append(line)
            flush()
            continue
        buffer.append(line)
    flush()
    return blocks


def chunk_text(text: str, *, size: int = 420, overlap: int = 60) -> list[Chunk]:
    """把文档切成块；``size``/``overlap`` 以字符计（中文≈token）。"""
    text = normalize(text)
    if not text:
        return []
    size = max(120, int(size))
    overlap = max(0, min(int(overlap), size // 2))

    chunks: list[Chunk] = []
    ordinal = 0
    for section, block in _split_blocks(text):
        sentences = split_sentences(block)
        if not sentences:
            continue
        current: list[str] = []
        current_len = 0
        part_index = 0

        def emit(parts: list[str], part_no: int) -> None:
            nonlocal ordinal
            body = SENTENCE_JOIN.join(parts).strip()
            if not body:
                return
            prefix = f"{section} " if section else ""
            locator = f"{prefix}§{part_no + 1}" if section else f"§{part_no + 1}"
            chunks.append(
                Chunk(
                    ordinal=ordinal,
                    text=body,
                    locator=locator.strip(),
                    token_count=_estimate_tokens(body),
                )
            )
            ordinal += 1

        for sentence in sentences:
            if current_len + len(sentence) > size and current:
                emit(current, part_index)
                part_index += 1
                # 用尾部句子做重叠
                tail: list[str] = []
                tail_len = 0
                for sentence_back in reversed(current):
                    if tail_len + len(sentence_back) > overlap:
                        break
                    tail.insert(0, sentence_back)
                    tail_len += len(sentence_back)
                current = tail
                current_len = tail_len
            current.append(sentence)
            current_len += len(sentence)
        emit(current, part_index)
    return chunks
