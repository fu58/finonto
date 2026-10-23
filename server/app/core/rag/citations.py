"""引用（Citation）构造与校验。

约束：回答中出现的 [cN] 必须能解析到真实 chunk；每条引用携带原文引句与定位，
并按「该句与原文的支撑度」标记 strong/weak，避免看起来像引用实则无依据。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ...models import Chunk, DocumentRecord
from ..text import overlap, split_sentences, truncate

MARKER = re.compile(r"\[(c\d+)\]", re.I)
SENT_END = re.compile(r"(?<=[。！？；!?;\n])")


@dataclass
class Citation:
    ref: str
    chunk_id: int
    doc_id: int
    doc_name: str
    doc_type: str = ""
    locator: str = ""
    page: int | None = None
    quote: str = ""
    statement: str = ""
    support: float = 0.0
    score: float = 0.0
    strength: str = "strong"
    rank: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_context_block(candidates) -> tuple[str, dict[str, Citation]]:  # noqa: ANN001
    """生成给 LLM 的编号资料块，同时建立 ref -> Citation 的映射。"""
    lines: list[str] = []
    index: dict[str, Citation] = {}
    for candidate in candidates:
        ref = candidate.ref or f"c{candidate.rank}"
        source = candidate.meta.get("doc_name", "") or f"文档 #{candidate.doc_id}"
        loc = candidate.locator or ""
        extra = []
        if candidate.meta.get("doc_type"):
            extra.append(str(candidate.meta.get("doc_type")))
        if candidate.meta.get("jurisdiction"):
            extra.append(str(candidate.meta.get("jurisdiction")))
        header = f"[{ref}] (来源: {source}"
        if extra:
            header += " | " + " / ".join(extra)
        if loc:
            header += f" | 定位: {loc})"
        else:
            header += ")"
        lines.append(header)
        lines.append(candidate.text.strip())
        lines.append("")
        index[ref] = Citation(
            ref=ref,
            chunk_id=candidate.chunk_id,
            doc_id=candidate.doc_id,
            doc_name=source,
            doc_type=str(candidate.meta.get("doc_type", "")),
            locator=loc,
            page=candidate.meta.get("page"),
            quote=best_quote("", candidate.text),
            score=round(candidate.final_score or candidate.fused_score, 4),
            rank=candidate.rank,
        )
    return "\n".join(lines), index


def best_quote(statement: str, chunk_text: str, *, max_len: int = 160) -> str:
    """从 chunk 中挑选最能支撑 statement 的一句作为引文。"""
    sentences = split_sentences(chunk_text)
    if not sentences:
        return truncate(chunk_text, max_len)
    if not statement:
        return truncate(sentences[0], max_len)
    best = max(sentences, key=lambda s: overlap(statement, s))
    return truncate(best, max_len)


def bind_citations(answer: str, index: dict[str, Citation], candidates) -> tuple[list[Citation], str]:  # noqa: ANN001
    """把回答句子与 [cN] 标记绑定，产出结构化引用并清洗非法标记。"""
    texts = {(c.ref or f"c{c.rank}"): c.text for c in candidates}
    used: dict[str, Citation] = {}
    cleaned_lines: list[str] = []
    for sentence in SENT_END.split(answer or ""):
        if not sentence:
            continue
        refs = [r.lower() for r in MARKER.findall(sentence)]
        valid_refs: list[str] = []
        for ref in refs:
            key = next((k for k in index if k.lower() == ref), None)
            if key is None:
                continue
            valid_refs.append(key)
            base = index[key]
            citation = used.get(key)
            if citation is None:
                citation = Citation(**{**asdict(base), "statement": truncate(sentence, 200)})
                used[key] = citation
            full_text = texts.get(key, base.quote)
            citation.quote = best_quote(sentence, full_text)
            citation.statement = citation.statement or truncate(sentence, 200)
            citation.support = round(max(citation.support, overlap(sentence, citation.quote)), 3)
            citation.strength = "strong" if citation.support >= 0.12 else "weak"
        stripped = MARKER.sub("", sentence)
        tail = " ".join(f"[{r}]" for r in dict.fromkeys(valid_refs))
        cleaned_lines.append(f"{stripped.rstrip()}{(' ' + tail) if tail else ''}")
    return list(used.values()), "\n".join(cleaned_lines).strip()


def coverage_stats(answer: str, citations: list[Citation]) -> dict[str, Any]:
    sentences = [s for s in split_sentences(answer) if len(s.strip()) > 8]
    if not sentences:
        return {"coverage": 0.0, "total_sentences": 0, "cited_sentences": 0, "weak": 0}
    cited = sum(1 for s in sentences if MARKER.search(s))
    weak = sum(1 for c in citations if c.strength == "weak")
    return {
        "coverage": round(cited / len(sentences), 3),
        "total_sentences": len(sentences),
        "cited_sentences": cited,
        "weak": weak,
    }


def hydrate(session, citations: list[Citation]) -> list[Citation]:  # noqa: ANN001
    """补齐文档名等真实字段（引用必须可回溯到库内记录）。"""
    for citation in citations:
        chunk = session.get(Chunk, citation.chunk_id)
        if chunk is not None:
            citation.locator = citation.locator or chunk.locator
            citation.page = citation.page or (chunk.meta or {}).get("page")
        doc = session.get(DocumentRecord, citation.doc_id)
        if doc is not None:
            citation.doc_name = citation.doc_name or doc.name
            citation.doc_type = citation.doc_type or doc.doc_type
    return citations
