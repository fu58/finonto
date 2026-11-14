"""生成：把「查询理解 + 检索结果 + 领域上下文」拼成提示词，流式产出答案。

提示词刻意使用固定的中文小节（【问题】【资料】【领域】），原因有三：
1. 人类可读，出问题时能直接看日志判断是检索错还是生成错；
2. mock 实现能稳定解析（零密钥路径不是靠魔法跑通的）；
3. 真实模型同样吃这套结构，切厂商不用改代码。
"""

from __future__ import annotations

from typing import Iterator

from ...providers.base import Message
from .citations import build_context
from .query_understanding import QueryPlan
from .retriever import RetrievedChunk

SYSTEM_PROMPT = """你是企业知识助手，回答必须严格基于给出的资料。
规则：
1. 只使用【资料】中的内容，不得引入外部知识；
2. 每句结论后面必须标注来源编号，如 [c1]、[c2]；
3. 资料不足以回答时，直接说「资料中没有相关依据」，不要猜测；
4. 定量问题必须给出具体数字与出处；对比问题分点列差异；
5. 用中文回答，简洁、可执行。"""

INTENT_GUIDE = {
    "definition": "用户在问「是什么 / 怎么定义」，先给定义，再补充关键属性与相关条款。",
    "quantitative": "用户在问数字/阈值/期限，必须给出具体数值，并说明出自哪一条。",
    "comparison": "用户在对比概念，用分点列出差异，每条差异都要有出处。",
    "ontology_generation": "用户在要求生成/补充本体结构，请归纳可建模的对象类型与属性线索。",
    "general": "按「结论 → 依据 → 注意事项」组织答案。",
}


def build_messages(
    plan: QueryPlan,
    chunks: list[RetrievedChunk],
    *,
    domain_context: str = "",
) -> tuple[list[Message], dict[str, RetrievedChunk]]:
    context, tag_map = build_context(chunks)
    sections = [
        "# 任务：answer",
        f"【问题】\n{plan.question}",
        f"【意图】\n{plan.intent}：{INTENT_GUIDE.get(plan.intent, INTENT_GUIDE['general'])}",
    ]
    if domain_context:
        sections.append(f"【领域】\n{domain_context}")
    if plan.sub_questions:
        sections.append("【子问题】\n" + "\n".join(f"- {q}" for q in plan.sub_questions))
    if plan.aligned_types:
        sections.append(
            "【已有类型】\n" + "、".join(item["label"] for item in plan.aligned_types)
        )
    if plan.filters:
        sections.append(f"【过滤条件】\n{plan.filters}")
    sections.append(f"【资料】\n{context or '（无检索结果）'}")
    sections.append("按规则作答，每个结论后标注 [cN]。")
    return [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(role="user", content="\n\n".join(sections)),
    ], tag_map


def generate(
    components,
    plan: QueryPlan,
    chunks: list[RetrievedChunk],
    *,
    domain_context: str = "",
    max_tokens: int = 900,
) -> tuple[str, dict[str, RetrievedChunk]]:
    messages, tag_map = build_messages(plan, chunks, domain_context=domain_context)
    answer = components.llm.chat(messages, max_tokens=max_tokens)
    return answer, tag_map


def generate_stream(
    components,
    plan: QueryPlan,
    chunks: list[RetrievedChunk],
    *,
    domain_context: str = "",
    max_tokens: int = 900,
) -> tuple[Iterator[str], dict[str, RetrievedChunk]]:
    messages, tag_map = build_messages(plan, chunks, domain_context=domain_context)
    return components.llm.chat_stream(messages, max_tokens=max_tokens), tag_map
