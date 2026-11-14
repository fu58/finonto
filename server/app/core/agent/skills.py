"""技能包：系统提示 + 工具白名单 + 输出契约 + 步数上限。

技能本身**领域中立**：不说「信贷」「设备」，只描述「怎么从文档抽结构」。
领域词表由当前空间的领域包在运行时注入（``packs.prompt_context_for``），
所以同一个技能在五个领域里都能用——这是「领域不写死」的关键一环。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Skill:
    key: str
    display: str
    description: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=list)
    output_contract: str = ""
    max_steps: int = 8

    def catalog(self) -> dict:
        return {
            "key": self.key,
            "display": self.display,
            "description": self.description,
            "allowed_tools": self.allowed_tools,
            "output_contract": self.output_contract,
            "max_steps": self.max_steps,
        }


BASE_RULES = """你是企业本体工程师。工作规则：
1. 先用 rag.search 取证，再下结论；没有证据不要新建类型；
2. 新建类型前必须调用 find_similar 检查重复；
3. 写本体只能通过 propose_type 产出提案，绝不假设提案会自动生效；
4. 每一步只调用一个工具，观察结果后再决定下一步；
5. 得出结论时用 Final Answer 收尾，说明「产出了什么提案 + 依据是什么」。"""


SKILLS: list[Skill] = [
    Skill(
        key="doc-extraction",
        display="文档结构抽取",
        description="从制度/规程中抽取候选对象类型、属性与约束，产出待确认提案。",
        system_prompt=BASE_RULES + "\n当前任务：文档结构抽取。优先覆盖「对象类型—属性—约束」三层。",
        allowed_tools=["rag.search", "doc.list", "doc.chunks", "ontology.list_types",
                       "find_similar", "propose_type", "wiki.list_pages"],
        output_contract="最终给出：新建提案数量、每个类型的名称与依据条款、需要人工补充的字段。",
        max_steps=8,
    ),
    Skill(
        key="clause-mapping",
        display="条款映射",
        description="把制度条款映射到已有对象类型与属性，找出缺字段与冲突。",
        system_prompt=BASE_RULES + "\n当前任务：条款映射。先列出已有类型，再逐条对齐，明确指出缺失与冲突。",
        allowed_tools=["rag.search", "ontology.list_types", "ontology.type_detail",
                       "find_similar", "propose_type"],
        output_contract="表格形式：条款 → 命中类型 → 命中属性 → 缺失/冲突结论。",
        max_steps=6,
    ),
    Skill(
        key="relation-discovery",
        display="关系发现",
        description="从语料中发现类型之间的语义关联，产出关联提案。",
        system_prompt=BASE_RULES + "\n当前任务：关系发现。只提两类关系：明确的（文本直述）与强推断的（有同类证据支持）。",
        allowed_tools=["rag.search", "ontology.list_types", "ontology.type_detail",
                       "graph.subgraph", "propose_type"],
        output_contract="按「源类型 —关系— 目标类型 —依据」列出，并标注置信度。",
        max_steps=7,
    ),
    Skill(
        key="def-refine",
        display="定义精修",
        description="针对某个类型，结合条款原文把定义、属性与约束改得更准确。",
        system_prompt=BASE_RULES + "\n当前任务：定义精修。保留原有语义，只做可被原文支撑的修改。",
        allowed_tools=["rag.search", "ontology.type_detail", "find_similar", "propose_type",
                       "wiki.read_page"],
        output_contract="给出修改前后的定义对照，以及每处修改的依据条款。",
        max_steps=6,
    ),
    Skill(
        key="graph-completion",
        display="图谱补全",
        description="找出孤立节点、缺证据类型与断链，提出补全动作。",
        system_prompt=BASE_RULES + "\n当前任务：图谱补全。优先处理孤立节点与无证据类型。",
        allowed_tools=["graph.subgraph", "ontology.list_types", "ontology.type_detail",
                       "rag.search", "propose_type"],
        output_contract="列出问题节点、建议动作（补关联/补证据/合并/删除）与优先级。",
        max_steps=6,
    ),
    Skill(
        key="web-research",
        display="联网查证",
        description="当本地资料不足时，联网查证并给出可引用的来源（默认关闭）。",
        system_prompt=BASE_RULES + "\n当前任务：联网查证。必须标注来源 URL，禁止把网络内容当作制度依据。",
        allowed_tools=["rag.search", "search.web", "memory.search", "propose_type"],
        output_contract="结论 + 本地依据（如有）+ 外部来源 URL 列表。",
        max_steps=6,
    ),
]

SKILL_MAP: dict[str, Skill] = {skill.key: skill for skill in SKILLS}


def get_skill(key: str | None) -> Skill:
    if key and key in SKILL_MAP:
        return SKILL_MAP[key]
    return SKILLS[0]


def catalog() -> list[dict]:
    return [skill.catalog() for skill in SKILLS]
