"""查询理解（Query Understanding）。

在检索之前完成：意图分类、口语化改写（结合对话上下文）、指代消解、同义扩展、
关键实体/术语加权、子问题分解、元数据过滤推断，以及「是否要生成本体」的判别。
规则层保证离线确定性；配置真实 LLM 时再叠加一层模型改写。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ...providers.base import ChatMessage
from ...providers.registry import registry
from ..text import candidate_terms, overlap, split_sentences, tokenize

# 基础同义表：与领域无关的口语↔书面口径归一，金融条目只在命中时生效。
# 当前空间的领域包同义表会在 expand() 里合并进来（同名 key 以领域包为准）。
SYNONYMS: dict[str, list[str]] = {
    "房贷": ["个人住房贷款", "住房按揭"],
    "逾期": ["欠款", "违约", "未按期偿还", "delinquency"],
    "不良": ["不良贷款", "非应计", "NPL", "五级分类次级及以上"],
    "敞口": ["风险暴露", "EAD", "违约风险暴露"],
    "评级": ["信用评级", "风险评级", "rating"],
    "KYC": ["客户尽职调查", "客户身份识别"],
    "反洗钱": ["AML", "洗钱风险", "可疑交易"],
    "资本充足率": ["资本充足", "CAR"],
    "拨备": ["贷款损失准备", "拨备覆盖率"],
    "担保": ["保证", "抵押", "质押"],
    "押品": ["抵质押物", "担保物", "collateral"],
    "额度": ["授信额度", "信用额度"],
    "利率": ["执行利率", "定价", "APR"],
    "客户": ["借款人", "存款人", "consumer"],
    "产品": ["金融产品", "服务"],
    "合约": ["合同", "协议"],
    "支付": ["付款", "结算"],
    "清算": ["清分", "结算"],
    "基金": ["公募基金", "资管产品"],
    "保单": ["保险合同"],
    "理赔": ["赔付", "给付"],
    "本体": ["ontology", "概念模型", "对象类型"],
}

REG_CUES = ("办法", "规定", "指引", "通知", "监管", "合规", "巴塞尔", "ifrs", "CAS", "JR/T", "GB/T", "《")
DEF_CUES = ("是什么", "定义", "含义", "指什么", "怎么理解", "什么是", "概念")
RELA_CUES = ("关系", "关联", "之间", "影响", "导致", "触发", "依赖", "属于", "包含", "区别", "对比", "差异")
ENUM_CUES = ("有哪些", "包括哪些", "列举", "分类", "类型有", "枚举", "分为")
PROC_CUES = ("怎么", "如何", "流程", "步骤", "办理", "操作", "申请", "审批")
GAP_CUES = ("缺什么", "还有哪些", "补充", "覆盖", "空白", "遗漏", "没有考虑")
GEN_CUES = (
    "生成", "创建", "建立", "构建", "抽取", "提炼", "识别出", "做成本体", "对象类型",
    "新增", "建模", "定义一下", "整理成", "补全", "设计",
)

_DATE = re.compile(r"(20\d{2})\s*(?:年|/|-)\s*(\d{1,2})?\s*(?:月)?\s*(\d{1,2})?")
_JURIS = re.compile(r"(银保监|人民银行|央行|证监会|外汇局|金融监管总局|反洗钱局|巴塞尔|欧盟|美国|中国|境内)")


@dataclass
class UnderstoodQuery:
    original: str
    rewritten: str = ""
    intent: str = "factual"
    answer_shape: str = "定义"
    expansions: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    terms: list[tuple[str, float]] = field(default_factory=list)
    sub_questions: list[str] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    wants_ontology: bool = False
    needs_web: bool = False
    language: str = "zh"
    known_types: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.6
    notes: list[str] = field(default_factory=list)

    @property
    def search_text(self) -> str:
        parts = [self.rewritten or self.original]
        parts += self.expansions[:6]
        parts += [t for t, _ in self.terms[:10]]
        return " ".join(p for p in parts if p)

    def to_dict(self) -> dict[str, Any]:
        return {
            "original": self.original,
            "rewritten": self.rewritten,
            "intent": self.intent,
            "answer_shape": self.answer_shape,
            "expansions": self.expansions,
            "entities": self.entities,
            "terms": [[t, round(s, 3)] for t, s in self.terms],
            "sub_questions": self.sub_questions,
            "filters": self.filters,
            "wants_ontology": self.wants_ontology,
            "needs_web": self.needs_web,
            "known_types": self.known_types,
            "confidence": round(self.confidence, 3),
            "notes": self.notes,
        }


def classify(query: str) -> tuple[str, str]:
    low = (query or "").lower()
    def hit(cues: tuple[str, ...]) -> bool:
        return any(c in low for c in cues)

    if hit(GAP_CUES) or hit(GEN_CUES):
        return "ontology_generation", "本体草稿"
    if hit(REG_CUES):
        return "regulation", "条款依据"
    if hit(ENUM_CUES):
        return "enumeration", "枚举清单"
    if hit(RELA_CUES):
        return "relation", "关系说明"
    if hit(PROC_CUES):
        return "procedure", "流程步骤"
    if hit(DEF_CUES):
        return "definition", "定义"
    if hit(("多少", "几", "阈值", "上限", "下限", "比例", "%")):
        return "quantitative", "数值与阈值"
    return "factual", "事实回答"


def expand(
    query: str,
    *,
    synonyms: dict[str, list[str]] | None = None,
    extra_suffixes: tuple[str, ...] = (),
) -> list[str]:
    low = (query or "").lower()
    out: list[str] = []
    for key, values in {**SYNONYMS, **(synonyms or {})}.items():
        if key.lower() in low:
            out.extend(values)
    # 中文 n-gram 尾缀扩展：贷款逾期 -> 逾期天数 / 逾期阶段
    for term, _score in candidate_terms(query, max_terms=6, extra_suffixes=extra_suffixes)[:4]:
        for suffix in ("定义", "分类", "阈值", "流程"):
            if suffix not in low:
                out.append(term + suffix)
    seen: set[str] = set()
    deduped: list[str] = []
    for item in out:
        if item and item not in seen and item not in low:
            seen.add(item)
            deduped.append(item)
    return deduped[:14]


def decompose(query: str) -> list[str]:
    sentences = split_sentences(query)
    parts: list[str] = []
    for sentence in sentences:
        for piece in re.split(r"[；;]|并且|同时|以及|另外|其次", sentence):
            piece = piece.strip("？?。 ，,")
            if len(piece) >= 6:
                parts.append(piece)
    return parts if len(parts) > 1 else ([query.strip()] if query.strip() else [])


def infer_filters(query: str) -> dict[str, Any]:
    low = (query or "").lower()
    filters: dict[str, Any] = {}
    doc_types = {
        "监管制度": ("办法", "规定", "指引", "监管", "ifrs", "cas", "jr/t", "《"),
        "合同模板": ("合同", "协议", "条款"),
        "产品说明": ("产品说明", "要素", "说明书"),
        "业务手册": ("手册", "操作规程", "流程"),
        "报表数据": ("报表", "明细", "数据"),
    }
    for doc_type, cues in doc_types.items():
        if any(c in low for c in cues):
            filters["doc_type"] = doc_type
            break
    if "《" in query or "办法" in query or "规定" in query:
        filters["min_authority"] = 0.6
    juris = _JURIS.search(query)
    if juris:
        filters["jurisdiction_hint"] = juris.group(1)
    year = _DATE.search(query)
    if year:
        filters["year_hint"] = int(year.group(1))
    return filters


def extract_entities(query: str, known_labels: list[str] | None = None) -> list[str]:
    """优先匹配本体中已有对象类型（用于「基于已有本体回答/对齐」）。"""
    found: list[str] = []
    for label in known_labels or []:
        if label and label in query:
            found.append(label)
    for m in re.finditer(r"《([^》]{2,40})》", query or ""):
        found.append(m.group(1))
    for m in re.finditer(r"([一-鿿]{2,10}(?:办法|规定|指引|标准|规范|条例))", query or ""):
        found.append(m.group(1))
    out: list[str] = []
    for item in found:
        if item not in out:
            out.append(item)
    return out[:10]


def match_known_types(query: str, types: list[tuple[int, str, str]]) -> list[dict[str, Any]]:
    """把问题里的表述对齐到现有对象类型，返回排序后的命中。"""
    matches: list[dict[str, Any]] = []
    for type_id, label, definition in types:
        if not label:
            continue
        score = 0.0
        if label in query:
            score = 1.0
        else:
            tokens = set(tokenize(label))
            if tokens and tokens <= set(tokenize(query)):
                score = 0.75
            elif overlap(label, query) > 0.5:
                score = 0.45
        if score:
            matches.append({"id": type_id, "label": label, "score": round(score, 3), "definition": definition[:80]})
    matches.sort(key=lambda m: -m["score"])
    return matches[:8]


def understand(
    query: str,
    *,
    history: list[dict[str, str]] | None = None,
    known_types: list[tuple[int, str, str]] | None = None,
    use_llm: bool | None = None,
    synonyms: dict[str, list[str]] | None = None,
    extra_suffixes: tuple[str, ...] = (),
) -> UnderstoodQuery:
    """规则层查询理解。`synonyms` / `extra_suffixes` 来自空间所属领域包，使扩展与
    术语加权跟随当前领域，而不是只有金融口径。"""
    query = (query or "").strip()
    intent, answer_shape = classify(query)
    expansions = expand(query, synonyms=synonyms, extra_suffixes=extra_suffixes)
    entities = extract_entities(query)
    sub_questions = decompose(query)
    filters = infer_filters(query)
    terms = candidate_terms(query, max_terms=16, extra_suffixes=extra_suffixes)
    known = match_known_types(query, known_types or [])

    notes: list[str] = []
    rewritten = _resolve_reference(query, history or [], notes)
    if answer_shape == "本体草稿" and not any(w in query for w in ("对象类型", "本体", "概念")):
        notes.append("识别为创建/补充本体意图，但问题中未显式提到本体，已按生成本体草稿处理")

    result = UnderstoodQuery(
        original=query,
        rewritten=rewritten,
        intent=intent,
        answer_shape=answer_shape,
        expansions=expansions,
        entities=entities,
        terms=terms,
        sub_questions=sub_questions,
        filters=filters,
        wants_ontology=intent == "ontology_generation",
        needs_web=bool(filters.get("year_hint")) or "最新" in query or "近期" in query,
        known_types=known,
        notes=notes,
        confidence=round(
            min(
                0.95,
                0.5 + 0.1 * bool(expansions) + 0.1 * bool(known) + 0.05 * len(sub_questions),
            ),
            3,
        ),
    )
    should_call_llm = use_llm if use_llm is not None else registry.settings.has_remote_llm
    if should_call_llm and registry.settings.llm_provider != "mock":
        result.notes.append("如需模型改写，请调用 understand_async")
    return result


async def understand_async(
    query: str,
    *,
    history: list[dict[str, str]] | None = None,
    known_types: list[tuple[int, str, str]] | None = None,
    synonyms: dict[str, list[str]] | None = None,
    extra_suffixes: tuple[str, ...] = (),
) -> UnderstoodQuery:
    """规则理解 + （配置了真实模型时）模型改写增强。"""
    result = understand(
        query,
        history=history,
        known_types=known_types,
        use_llm=False,
        synonyms=synonyms,
        extra_suffixes=extra_suffixes,
    )
    if registry.settings.llm_provider != "mock" and registry.settings.has_remote_llm:
        await _llm_augment(result)
    return result


def _resolve_reference(query: str, history: list[dict[str, str]], notes: list[str]) -> str:
    """把「它的定义」「这类对象」之类的指代补全为上一轮的主题实体。"""
    if not history:
        return query
    pronouns = ("它", "他们", "这类", "该", "此", "上述", "这些", "同一个")
    if not any(p in query for p in pronouns):
        return query
    for turn in reversed(history):
        text = turn.get("content") or turn.get("question") or ""
        for m in re.finditer(r"([一-鿿]{2,12})(?:的|是|指)", text):
            anchor = m.group(1)
            if anchor in ("什么", "哪些", "如何", "怎么", "可以"):
                continue
            notes.append(f"指代消解：将「{anchor}」补全进改写问题")
            return f"{query}（此处指 {anchor}）"
    return query


def _llm_augment(result: UnderstoodQuery) -> None:
    prompt = (
        "[[TASK:query_understand]]\n"
        "请对下面的用户问题进行查询理解，只输出 JSON，字段："
        "rewritten(str), intent(str), sub_questions(list), expansions(list), filters(object), wants_ontology(bool)。\n\n"
        f"## 原始问题\n{result.original}\n\n"
        f"## 已知本体对象类型\n{json.dumps([k['label'] for k in result.known_types], ensure_ascii=False)}\n"
    )
    try:
        import asyncio

        provider = registry.llm()
        raw, _usage = asyncio.get_event_loop().run_until_complete(
            provider.complete([ChatMessage(role="user", content=prompt)], task="query_understand", json_mode=True)
        )
        payload = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except Exception:  # noqa: BLE001  增强失败不影响规则结果
        result.notes.append("模型改写不可用，已使用规则结果")
        return
    if isinstance(payload, dict):
        result.rewritten = payload.get("rewritten") or result.rewritten
        result.expansions = list(dict.fromkeys([*result.expansions, *(payload.get("expansions") or [])]))[:16]
        result.sub_questions = payload.get("sub_questions") or result.sub_questions
        result.wants_ontology = bool(payload.get("wants_ontology", result.wants_ontology))
        for key, value in (payload.get("filters") or {}).items():
            result.filters.setdefault(key, value)
