"""领域包：通用（空白域）。

不预设任何词表、基线类型与示例语料。两个用途：
1. 用户新建一个「还没想好领域」的空间时的默认落点；
2. 遇到 domain 无法识别时的安全回落——绝不能让服务因为「不认识这个领域」而报错。

空白域的价值在于证明：领域能力是**从语料与人工确认里长出来的**，不是靠预置字典撑起来的。
"""

from __future__ import annotations

from .base import DomainPack

PACK = DomainPack(
    key="general",
    label="通用（未预设领域）",
    summary="空白域：不预设类型与词表，完全由入库语料 + 人工确认生成。",
    space_name="通用本体域",
    space_iri="https://finonto.local/ontology/general",
    space_description="用于承载尚未归类的知识资产。不预置任何对象类型与术语线索，"
    "所有类型都来自语料抽取与人工确认，可作为「从零开始建模」的起点。",
    business_domains=[],
    object_types=[],
    links=[],
    samples=[],
    term_suffixes=(),
    clause_markers=("第", "条", "款"),
    domain_cues=(),
    tag_cues=(),
    synonyms={},
    standards=[],
    classification=[],
    facets=[],
    sample_questions=[
        "这批文档里反复出现的关键概念有哪些？",
        "把出现频率最高的三个概念整理成候选对象类型。",
    ],
    agent_hint="该空间没有预置词表，请更多依赖语料本身与已有类型。",
)
