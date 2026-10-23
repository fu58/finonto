"""流式生成与引用绑定。

生成过程严格 grounded：资料块以 [cN] 编号提供给模型，要求逐句标注来源；
输出结束后统一做标记清洗（丢弃无法解析的引用）、支撑度打分与覆盖度统计。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.orm import Session

from ...providers.base import ChatMessage
from ...providers.registry import registry
from ..text import truncate
from .citations import Citation, bind_citations, build_context_block, coverage_stats, hydrate
from .query_understanding import UnderstoodQuery

ANSWER_RULES = (
    "1. 只能依据【资料】中的内容作答，不得使用资料之外的知识补齐事实；\n"
    "2. 每个陈述句末尾用 [cN] 标注支撑该句的资料编号，一句可标注多个；\n"
    "3. 资料不足以回答时，明确说明缺少哪些信息，并建议补充的文档类型；\n"
    "4. 涉及制度、标准或规范口径时优先给出文号、条款号或标准名称；\n"
    "5. 不要输出资料原文之外的数字、比例或期限。"
)

SHAPE_HINTS = {
    "定义": "先给出一句可引用的规范定义，再补充关键限定条件。",
    "枚举清单": "用列表逐项列出，每项后标注来源。",
    "关系说明": "说明方向性（A 对 B 的作用），并指出触发条件或约束。",
    "条款依据": "引用具体条款或文号，并说明适用对象。",
    "流程步骤": "按顺序给出步骤，标注每步的依据。",
    "数值与阈值": "给出具体数值、单位与阈值来源，避免笼统表述。",
    "本体草稿": "先回答现有资料对该概念的支撑情况，再列出建议新增的对象类型候选。",
    "事实回答": "直接回答事实，避免多余铺陈。",
}


def build_messages(
    understood: UnderstoodQuery,
    candidates: list[Any],
    context_block: str,
    *,
    known_types: list[str] | None = None,
    extra: str = "",
    domain_label: str = "",
) -> list[ChatMessage]:
    shape_hint = SHAPE_HINTS.get(understood.answer_shape, "")
    role = f"你是「{domain_label}」领域的知识库问答助手" if domain_label else "你是本体知识库的问答助手"
    system = (
        "[[TASK:rag_answer]]\n"
        f"{role}，负责基于检索到的资料回答问题。\n"
        f"回答规则：\n{ANSWER_RULES}\n"
        f"回答形态：{understood.answer_shape}。{shape_hint}\n"
    )
    parts = [f"## 用户问题\n{understood.rewritten or understood.original}"]
    if understood.sub_questions and len(understood.sub_questions) > 1:
        parts.append("## 需要覆盖的子问题\n" + "\n".join(f"- {q}" for q in understood.sub_questions))
    if known_types:
        parts.append("## 当前本体已有对象类型（避免重复造名）\n" + "、".join(known_types[:60]))
    parts.append(f"## 资料\n{context_block}")
    if extra:
        parts.append(f"## 附加要求\n{extra}")
    return [ChatMessage(role="system", content=system), ChatMessage(role="user", content="\n\n".join(parts))]


async def stream_answer(
    understood: UnderstoodQuery,
    candidates: list[Any],
    *,
    session: Session | None = None,
    known_types: list[str] | None = None,
    extra: str = "",
    answer_id: str = "",
    domain_label: str = "",
) -> AsyncIterator[dict[str, Any]]:
    """SSE 事件流：context -> token* -> citations -> done。"""
    context_block, index = build_context_block(candidates)
    messages = build_messages(
        understood, candidates, context_block, known_types=known_types, extra=extra, domain_label=domain_label
    )
    yield {
        "type": "context",
        "answer_id": answer_id,
        "question": understood.original,
        "understood": understood.to_dict(),
        "context_chars": sum(len(c.text) for c in candidates),
        "candidates": [c.as_dict() for c in candidates],
    }
    provider = registry.llm()
    buffer: list[str] = []
    try:
        async for piece in provider.stream(messages, task="rag_answer"):
            buffer.append(piece)
            yield {"type": "token", "text": piece}
    except Exception as exc:  # noqa: BLE001  流式失败要给出可诊断事件而不是 500
        yield {"type": "error", "message": f"生成中断: {exc}"}
    raw_answer = "".join(buffer)
    citations, answer = bind_citations(raw_answer, index, candidates)
    if session is not None:
        citations = hydrate(session, citations)
    stats = coverage_stats(answer, citations)
    if not candidates:
        stats["note"] = "未检索到资料"
    used_refs = {c.ref for c in citations}
    yield {
        "type": "citations",
        "citations": [c.as_dict() for c in citations],
        "unused_refs": [ref for ref in index if ref not in used_refs],
    }
    yield {
        "type": "done",
        "answer_id": answer_id,
        "answer": answer,
        "coverage": stats,
        "provider": type(provider).__name__,
        "citation_count": len(citations),
    }


async def answer_once(
    understood: UnderstoodQuery,
    candidates: list[Any],
    *,
    session: Session | None = None,
    known_types: list[str] | None = None,
    extra: str = "",
    answer_id: str = "",
    domain_label: str = "",
) -> dict[str, Any]:
    """非流式收集（供 Agent 工具与批处理调用）。"""
    chunks: list[str] = []
    citations: list[Citation] = []
    stats: dict[str, Any] = {}
    error = ""
    async for event in stream_answer(
        understood,
        candidates,
        session=session,
        known_types=known_types,
        extra=extra,
        answer_id=answer_id,
        domain_label=domain_label,
    ):
        if event["type"] == "token":
            chunks.append(event["text"])
        elif event["type"] == "citations":
            citations = event["citations"]
        elif event["type"] == "done":
            stats = event["coverage"]
        elif event["type"] == "error":
            error = event["message"]
    return {
        "answer": "".join(chunks),
        "citations": citations,
        "coverage": stats,
        "error": error,
        "context_preview": truncate(" ".join(chunks), 200),
    }
