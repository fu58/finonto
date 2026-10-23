"""本体建模词汇：与领域无关的内核词汇（对象类型分类、数据类型、关系类别、约束类型）。

采用 OWL 风格（Class / DatatypeProperty / ObjectProperty / SHACL 约束）。领域特有的维度
——业务子域、数据分级、标准目录、注解维度——不再写死在这里，而是由 `packs/` 下的
领域包声明，再经 `packs.vocab_for(session, space_id)` 合并后交给前端与 Agent。
"""

from __future__ import annotations

OBJECT_KINDS = {
    "class": "抽象概念类（owl:Class）",
    "entity": "可实例化的业务实体，有唯一标识",
    "event": "在时间上发生的业务事件/风险事件",
    "document": "凭证、单据、合同文本类对象",
    "product": "产品/服务/可交付物",
    "party": "参与主体（机构、人员、对手方）",
    "agreement": "合约/协议/条款",
    "attribute_domain": "取值域/分类体系",
    "process": "流程/活动",
    "metric": "指标/度量",
    "regulation": "规则/制度/标准对象",
}

PROPERTY_DATATYPES = {
    "string": "xsd:string",
    "text": "rdfs:comment",
    "integer": "xsd:integer",
    "decimal": "xsd:decimal",
    "money": "onto:Money",
    "percentage": "onto:Percentage",
    "date": "xsd:date",
    "datetime": "xsd:dateTime",
    "duration": "xsd:duration",
    "boolean": "xsd:boolean",
    "enum": "rdfs:Literal (受控枚举)",
    "identifier": "onto:Identifier",
    "uri": "xsd:anyURI",
    "json": "onto:JSON",
}

# 关联语义分类：决定图谱边的显示样式与推理规则；领域包可追加自己的关系族
RELATION_CATEGORIES = {
    "taxonomy": "分类/上下位（subClassOf 家族）",
    "part_of": "组成/包含",
    "participant": "参与方/主体关系",
    "reference": "引用/指向（外键式）",
    "causal": "引发/触发",
    "regulatory": "受规则约束/依据",
    "derivation": "派生/计算来源",
    "association": "一般业务关联",
}

CONSTRAINT_KINDS = {
    "required": "必填槽位",
    "cardinality": "基数约束（min/max）",
    "range": "取值范围",
    "enum": "受控词表",
    "pattern": "格式/正则",
    "unique": "唯一性",
    "codependency": "条件依赖（若 A 则 B）",
    "consistency": "跨属性一致性",
    "regulatory": "外部规则口径约束",
}

SEVERITIES = {"violation": "阻断", "warning": "告警", "info": "提示"}


def kind_label(kind: str) -> str:
    return OBJECT_KINDS.get(kind, kind)


def datatype_iri(datatype: str) -> str:
    return PROPERTY_DATATYPES.get(datatype, "xsd:string")
