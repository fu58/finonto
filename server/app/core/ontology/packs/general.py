"""内置领域包：通用（不预设领域）。

给「还没打包成领域」的文档用：空白基线 + 中性词表，对象类型完全由文档解析与 Agent 推理产出。
新建空间时默认落到这里，也是 `pack_for_space` 认不出 `space.domain` 时的回落项。
"""

from __future__ import annotations

from .base import DomainPack

SPACE_NAME = "通用本体域"

PACK = DomainPack(
    key="general",
    label="通用（自定义领域）",
    summary="不预设领域词表的空白本体域：基线类型为空，对象类型与关联全部由文档解析和 Agent 推理产出。",
    space_name=SPACE_NAME,
    space_iri="https://onto.example.com/ontology/general#",
    space_description="无预设领域的空白本体空间，用于承载任意新领域文档并按需增量建模。",
    business_domains=["通用", "组织", "流程", "对象", "指标", "规范"],
    object_types=[],
    links=[],
    samples=[],
    facets={
        "source_refs": "定义出处（文档/条款/章节）",
        "owner_role": "责任角色",
        "lifecycle": "生命周期状态",
        "data_classification": "数据敏感分级",
    },
    standards=[],
    classification={
        "public": "公开（可对外）",
        "internal": "内部（组织内可用）",
        "restricted": "受限（需授权）",
        "confidential": "机密（最小可见）",
    },
    term_suffixes=(
        "系统", "流程", "角色", "活动", "任务", "记录", "单据", "文档", "规范", "标准",
        "指标", "阈值", "参数", "资源", "设备", "组件", "事件", "状态", "版本", "清单",
    ),
    clause_markers=("应当", "必须", "不得", "仅限", "至少", "不超过"),
    domain_cues={},
    tag_cues={},
    synonyms={},
    sample_questions=[
        "这批资料里出现了哪些核心概念，它们之间是什么关系？",
        "资料里有哪些带数值口径的要求或阈值？",
        "当前文档中的术语分别属于哪个子域？",
    ],
    agent_hint=(
        "建模侧重：这是未预设领域，先从文档里归纳出稳定名词作为对象类型，"
        "再补父子层级与关联；business_domain 用文档里实际出现的子域名，不要套用其它领域的词表。"
    ),
)
