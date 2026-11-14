"""本体枚举与中文标签：单一事实来源。

前后端、导出、校验都从这里取标签，避免「同一概念三种叫法」。
"""

from __future__ import annotations

KIND_LABELS: dict[str, str] = {
    "class": "概念类",
    "agreement": "合同/协议",
    "role": "角色",
    "document": "文档/制度",
    "metric": "指标/度量",
    "event": "事件",
    "organization": "组织/场所",
    "asset": "资产/设备",
    "process": "流程/活动",
}

RELATION_CATEGORY_LABELS: dict[str, str] = {
    "taxonomy": "分类关系",
    "participant": "参与关系",
    "reference": "引用关系",
    "derivation": "派生关系",
    "regulatory": "监管关系",
    "part_of": "组成关系",
    "causal": "因果关系",
}

PROPERTY_TYPE_LABELS: dict[str, str] = {
    "string": "文本",
    "integer": "整数",
    "decimal": "小数",
    "boolean": "布尔",
    "date": "日期",
    "datetime": "日期时间",
    "uri": "链接",
    "enum": "枚举",
}

CONSTRAINT_KIND_LABELS: dict[str, str] = {
    "range": "取值范围",
    "codependency": "协同依赖",
    "cardinality": "基数约束",
}

SEVERITY_LABELS: dict[str, str] = {
    "violation": "违规",
    "warning": "警告",
    "info": "提示",
}

PROPOSAL_ACTION_LABELS: dict[str, str] = {
    "create_type": "新建对象类型",
    "update_type": "修改对象类型",
    "create_link": "新建关联",
    "create_constraint": "新建约束",
    "update_property": "修改属性",
}

PROPOSAL_STATUS_LABELS: dict[str, str] = {
    "pending": "待确认",
    "accepted": "已采纳",
    "rejected": "已驳回",
}

PAGE_TYPE_LABELS: dict[str, str] = {
    "index": "索引页",
    "type": "类型页",
    "domain": "业务域页",
    "relations": "关联目录",
    "quality": "质量报告",
}


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def relation_label(category: str) -> str:
    return RELATION_CATEGORY_LABELS.get(category, category)


def severity_label(severity: str) -> str:
    return SEVERITY_LABELS.get(severity, severity)


def action_label(action: str) -> str:
    return PROPOSAL_ACTION_LABELS.get(action, action)


def status_label(status: str) -> str:
    return PROPOSAL_STATUS_LABELS.get(status, status)


def page_type_label(page_type: str) -> str:
    return PAGE_TYPE_LABELS.get(page_type, page_type)
