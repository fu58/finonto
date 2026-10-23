"""技能（Skill）：把「系统提示 + 允许的工具集 + 输出契约」打包成可复用能力。

Agent 选择技能后即受该工具白名单与输出结构约束，便于把本体建模的常见工序
（文档抽取、条款映射、关系发现、定义精化、图谱补全）固化下来。技能本身与
领域无关：具体领域的子域、标准与建模侧重由空间所属的领域包在提示词里注入。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Skill:
    name: str
    display: str
    description: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=list)
    output_contract: dict[str, Any] = field(default_factory=dict)
    max_steps: int = 8
    example: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


BASE_RULES = (
    "你是本体建模 Agent，工作领域由提示词中的【领域上下文】给出。工作准则：\n"
    "1. 每一步先给出简短思考（Thought），再选择一个工具（Action）并给出 JSON 参数；\n"
    "2. 只能使用【可用工具】列表中的工具，参数必须符合 JSON 结构；\n"
    "3. 结论必须有依据：涉及文档时引用 chunk 定位，涉及概念时对齐已有对象类型；\n"
    "4. 不臆造制度、标准或阈值数值；若资料缺失，明确说明缺口；\n"
    "5. 新增对象类型一律走 ontology.propose_type（生成待人工确认的提案），不直接写入本体。"
)

SKILLS: list[Skill] = [
    Skill(
        name="doc-extraction",
        display="文档本体抽取",
        description="解析知识库文档，识别候选对象类型、定义、属性与制度/标准约束，产出可确认的本体提案。",
        system_prompt=BASE_RULES
        + "\n\n本技能侧重：从原文中抽取「X 是指 …」的定义句式、数值阈值与受控词表，"
        "为每个候选概念附上证据引用；同名的已有类型应复用而非新建。",
        allowed_tools=[
            "doc.list",
            "doc.read",
            "rag.search",
            "ontology.list_types",
            "ontology.find_similar",
            "ontology.propose_type",
            "ontology.validate",
            "memory.recall",
            "finish",
        ],
        output_contract={
            "type": "object",
            "required": ["answer"],
            "properties": {
                "answer": {"type": "string", "description": "结论摘要"},
                "proposals": {"type": "integer", "description": "生成的提案数量"},
            },
        },
        max_steps=10,
        example="从本领域的制度或标准原文中抽取对象类型与约束",
    ),
    Skill(
        name="clause-mapping",
        display="条款与标准映射",
        description="把制度、监管或标准条款映射到本体对象类型/属性/约束，标注出处、生效日期与数据分级。",
        system_prompt=BASE_RULES
        + "\n\n本技能侧重：检索条款原文，把每一条义务/阈值转成 Constraint 提案（kind=regulatory），"
        "并为受影响的对象类型补充 annotations 中的条款依据与数据分级。",
        allowed_tools=[
            "rag.search",
            "doc.list",
            "ontology.list_types",
            "ontology.get_type",
            "ontology.propose_constraint",
            "ontology.propose_update",
            "memory.remember",
            "finish",
        ],
        max_steps=9,
        example="把某项标准里的分级与阈值要求映射到对应对象类型",
    ),
    Skill(
        name="relation-discovery",
        display="关联关系发现",
        description="在已有对象类型之间发现语义关联（上下位/组成/参与/因果/监管），并写入关联提案。",
        system_prompt=BASE_RULES
        + "\n\n本技能侧重：先用 rag.search 找到共现证据，再判定关系类别（taxonomy/part_of/participant/"
        "causal/regulatory/derivation），避免把外键关系误标为分类关系。",
        allowed_tools=[
            "ontology.list_types",
            "rag.search",
            "rag.read_chunk",
            "ontology.propose_link",
            "graph.subgraph",
            "sandbox.run_python",
            "finish",
        ],
        max_steps=8,
        example="找出核心实体之间的上下位、组成与因果关系",
    ),
    Skill(
        name="def-refine",
        display="定义精化",
        description="针对定义薄弱或口径冲突的对象类型，检索原文精化定义并给出属性/枚举补全建议。",
        system_prompt=BASE_RULES
        + "\n\n本技能侧重：读取 ontology.validate 报告中的 definition_missing / no_property 问题，"
        "逐个补齐定义、单位、枚举成员与必填约束。",
        allowed_tools=[
            "ontology.validate",
            "ontology.get_type",
            "rag.search",
            "rag.read_chunk",
            "ontology.propose_update",
            "wiki.read_page",
            "finish",
        ],
        max_steps=8,
        example="补齐定义薄弱概念的定义、单位与枚举",
    ),
    Skill(
        name="graph-completion",
        display="知识图谱补全",
        description="从结构化数据源与图谱结构出发补全对象类型与关联，并统计覆盖率。",
        system_prompt=BASE_RULES
        + "\n\n本技能侧重：用 datasource.profile 读取表结构与样本，把表映射为对象类型、外键映射为关联；"
        "并用 graph.stats / graph.subgraph 找出孤立节点。",
        allowed_tools=[
            "datasource.describe",
            "datasource.profile",
            "graph.stats",
            "graph.subgraph",
            "ontology.propose_type",
            "ontology.propose_link",
            "sandbox.run_python",
            "finish",
        ],
        max_steps=10,
        example="用数据字典或表结构文件补全本体的类型与关联",
    ),
    Skill(
        name="web-research",
        display="联网补充调研",
        description="当本地知识库不足时，检索公开的制度与标准资料，把要点沉淀为记忆与提案。",
        system_prompt=BASE_RULES
        + "\n\n本技能侧重：先确认本地检索为空，再用 search.web 检索官方来源（监管机构、标准发布机构），"
        "抓取正文后抽取概念，最后 memory.remember 沉淀结论。",
        allowed_tools=[
            "rag.search",
            "search.web",
            "search.fetch",
            "ontology.propose_type",
            "memory.remember",
            "finish",
        ],
        max_steps=8,
        example="检索本领域最新标准或规范的征求意见稿要点",
    ),
]

SKILL_INDEX = {skill.name: skill for skill in SKILLS}


def get_skill(name: str | None) -> Skill | None:
    if not name:
        return None
    return SKILL_INDEX.get(name)


def default_skill() -> Skill:
    return SKILLS[0]


def allowed_tools_for(name: str | None) -> list[str] | None:
    skill = get_skill(name)
    return skill.allowed_tools if skill else None
