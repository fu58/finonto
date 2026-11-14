"""查询理解：意图、同义扩展、子问题拆分、结构化过滤、与已有类型对齐。

这五件产物**各自被下游真正使用**（不是摆设）：
* intent → 生成策略与提示词（定量问题要求给出数字与出处）；
* 同义扩展 → BM25 查询词扩充；
* 子问题 → 多轮检索的查询列表；
* filters → 候选片段的结构化裁剪（文种/辖区/最低权威度）；
* aligned_types → 图感知加权（命中已有类型时提升相关片段）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select

from ...models import ObjectType
from ..text import normalize, term_set

INTENT_RULES: tuple[tuple[str, str], ...] = (
    ("ontology_generation", r"生成|设计|建模|抽取|补充.*(类型|本体|结构)|构建.*本体"),
    ("quantitative", r"多少|比例|百分比|金额|上限|下限|期限|天数|阈值|几个|多大"),
    ("comparison", r"对比|区别|差异|相比|不同|优劣"),
    ("definition", r"是什么|是指|定义|含义|什么是|解释"),
    ("general", r"."),
)

DOC_TYPE_HINTS: dict[str, str] = {
    "制度": "制度", "办法": "制度", "规定": "制度",
    "标准": "标准", "规范": "标准",
    "合同": "合同", "协议": "合同",
    "手册": "手册",
    "规程": "规程",
}

CLAUSE_HINTS = ("第", "条", "款", "附件")

#: 「拿不准就多召回」——子问题拆分的连接符
SUBQ_SPLIT = re.compile(r"[；;]|并且|以及|同时|此外|另外|还有")


@dataclass(slots=True)
class QueryPlan:
    question: str
    intent: str = "general"
    sub_questions: list[str] = field(default_factory=list)
    filters: dict = field(default_factory=dict)
    expanded_terms: list[str] = field(default_factory=list)
    aligned_types: list[dict] = field(default_factory=list)
    rationale: str = ""
    used_llm: bool = False

    def as_dict(self) -> dict:
        return {
            "question": self.question,
            "intent": self.intent,
            "sub_questions": self.sub_questions,
            "filters": self.filters,
            "expanded_terms": self.expanded_terms,
            "aligned_types": self.aligned_types,
            "rationale": self.rationale,
            "used_llm": self.used_llm,
        }


def classify_intent(question: str) -> str:
    for intent, pattern in INTENT_RULES:
        if re.search(pattern, question):
            return intent
    return "general"


def extract_filters(question: str) -> dict:
    """只抽取**明确的**结构化过滤，避免把题目里的名词误当筛选条件。

    反例（必须避免）：「信贷合同中年利率上限是多少？」里出现「合同」，
    若据此过滤 ``doc_type=合同``，会把制度类文档全部排除，直接检索为空。
    所以只有出现「仅/只/限于/查找 + 文种」或「文种 + 中/里/内」这类**筛选句式**才生效。
    """
    filters: dict = {}
    doc_types: list[str] = []
    for pattern in (
        r"(?:仅|只|限于|只看|仅看|查找|检索)\s*(制度|标准|合同|协议|手册|规程)",
        r"按(?:照)?(制度|标准|规程)(?:要求|规定|执行)",
    ):
        for match in re.finditer(pattern, question):
            doc_type = DOC_TYPE_HINTS.get(match.group(1))
            if doc_type and doc_type not in doc_types:
                doc_types.append(doc_type)
    if doc_types:
        filters["doc_types"] = doc_types

    if any(word in question for word in ("监管", "合规", "法定", "必须遵守")):
        filters["min_authority"] = 4
    if re.search(r"最新|现行|最近修订", question):
        filters["prefer_recent"] = True
    if "集团" in question:
        filters["jurisdiction"] = "集团"
    if any(word in question for word in ("全国", "国家")):
        filters.setdefault("jurisdiction", "全国")
    return filters


def split_sub_questions(question: str) -> list[str]:
    parts = [p.strip(" ，,。?？") for p in SUBQ_SPLIT.split(question)]
    parts = [p for p in parts if len(p) >= 4]
    return parts if len(parts) > 1 else []


def expand_terms(question: str, synonyms: dict[str, list[str]]) -> list[str]:
    expanded: list[str] = []
    for keyword, alternatives in synonyms.items():
        if keyword in question:
            for alt in alternatives:
                if alt not in question and alt not in expanded:
                    expanded.append(alt)
    return expanded


def align_types(session, space_id: int, question: str, limit: int = 5) -> list[dict]:
    """把问题对准已有对象类型：标签/别名命中优先，其次 token 重叠。"""
    types = session.execute(
        select(ObjectType).where(ObjectType.space_id == space_id)
    ).scalars().all()
    if not types:
        return []
    q_terms = term_set(question)
    scored: list[tuple[float, ObjectType]] = []
    for item in types:
        score = 0.0
        if item.label and item.label in question:
            score += 1.0
        for alias in item.aliases or []:
            if alias and alias in question:
                score += 0.8
                break
        label_terms = term_set(item.label or "")
        if label_terms:
            score += 0.6 * len(q_terms & label_terms) / len(label_terms)
        if score > 0.25:
            scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {
            "type_id": item.id,
            "label": item.label,
            "score": round(score, 3),
            "business_domain": item.business_domain,
        }
        for score, item in scored[:limit]
    ]


def understand(
    session,
    space_id: int,
    question: str,
    *,
    synonyms: dict[str, list[str]] | None = None,
    llm=None,
) -> QueryPlan:
    question = normalize(question)
    plan = QueryPlan(question=question)
    plan.intent = classify_intent(question)
    plan.filters = extract_filters(question)
    plan.sub_questions = split_sub_questions(question)
    plan.expanded_terms = expand_terms(question, synonyms or {})
    plan.aligned_types = align_types(session, space_id, question)

    reasons = [f"规则判定意图为 {plan.intent}"]
    if plan.aligned_types:
        reasons.append("命中已有类型 " + "、".join(t["label"] for t in plan.aligned_types))
    if plan.expanded_terms:
        reasons.append("同义扩展 " + "、".join(plan.expanded_terms))
    if plan.filters:
        reasons.append(f"过滤条件 {plan.filters}")
    plan.rationale = "；".join(reasons)

    # 有真实模型时再做一次增强（mock 不做，保持确定性）
    if llm is not None and getattr(llm, "name", "") != "mock" and "finonto-mock" not in str(getattr(llm, "name", "")):
        try:
            from ...providers.base import Message

            raw = llm.chat(
                [
                    Message(
                        role="user",
                        content=(
                            "# 任务：query_understanding\n"
                            f"【问题】\n{question}\n"
                            '请输出 JSON：{"intent":"...","synonyms":{"词":["同义词"]}}'
                        ),
                    )
                ],
                max_tokens=200,
            )
            data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
            if data.get("intent"):
                plan.intent = data["intent"]
            for keyword, alternatives in (data.get("synonyms") or {}).items():
                for alt in alternatives or []:
                    if alt not in plan.expanded_terms:
                        plan.expanded_terms.append(alt)
            plan.used_llm = True
            plan.rationale += "；LLM 增强已生效"
        except Exception:  # noqa: BLE001 - 增强失败必须静默退回规则
            pass
    return plan
