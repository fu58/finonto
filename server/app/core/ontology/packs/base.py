"""领域包（Domain Pack）：把一个本体领域的词表、基线类型、关联与示例语料打包成一个可安装单元。

平台不再把「金融」写死在代码里：金融只是内置的领域包之一，维修、人工智能、计算机网络
同理。新增一个领域 = 在本目录放一个模块，声明一个 DomainPack，无需改动服务层与前端。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 关联局部名 → 关系类别：跨领域通用的骨架，领域包可用 link_categories 补充自己的局部名
COMMON_LINK_CATEGORIES: dict[str, str] = {
    "subClassOf": "taxonomy",
    "partOf": "part_of",
    "hasPart": "part_of",
    "references": "reference",
    "isDefinedBy": "regulatory",
    "hasParticipant": "participant",
    "triggers": "causal",
    "derivedFrom": "derivation",
    "associatedWith": "association",
    "requires": "regulatory",
    "constrains": "regulatory",
    "evidences": "reference",
    "records": "reference",
}


@dataclass
class SampleDoc:
    """随领域包发布的示例语料（位于 data/samples/<pack_key>/ 下）。"""

    filename: str
    doc_type: str = "参考资料"
    issuer: str = ""
    jurisdiction: str = ""
    authority: float = 0.6


@dataclass
class DomainPack:
    key: str
    label: str
    summary: str
    space_name: str
    space_iri: str
    space_description: str
    business_domains: list[str]
    object_types: list[dict[str, Any]]
    links: list[tuple[str, str, str, str]]
    samples: list[SampleDoc] = field(default_factory=list)
    # 词表扩展：在通用 OBJECT_KINDS / RELATION_CATEGORIES 之上叠加本领域常用维度
    extra_kinds: dict[str, str] = field(default_factory=dict)
    extra_relation_categories: dict[str, str] = field(default_factory=dict)
    facets: dict[str, str] = field(default_factory=dict)
    standards: list[str] = field(default_factory=list)
    classification: dict[str, str] = field(default_factory=dict)
    link_categories: dict[str, str] = field(default_factory=dict)
    # 供离线术语抽取与 Mock 推理使用的领域线索
    term_suffixes: tuple[str, ...] = ()
    clause_markers: tuple[str, ...] = ()
    domain_cues: dict[str, tuple[str, ...]] = field(default_factory=dict)
    tag_cues: dict[str, str] = field(default_factory=dict)
    synonyms: dict[str, list[str]] = field(default_factory=dict)
    # 问答页签的起手问题：跟着领域包走，切空间就不再是金融问题打头
    sample_questions: list[str] = field(default_factory=list)
    agent_hint: str = ""

    def category_of(self, local_name: str) -> str:
        return self.link_categories.get(local_name) or COMMON_LINK_CATEGORIES.get(local_name, "association")

    def sample_dir(self, server_root: Path) -> Path:
        return server_root / "data" / "samples" / self.key

    def guess_domain(self, text: str) -> str:
        """按领域线索把一段文本归到某个业务子域，供离线抽取与提案兜底使用。"""
        body = text or ""
        for domain, cues in self.domain_cues.items():
            if any(cue in body for cue in cues):
                return domain
        return "通用" if "通用" in self.business_domains else (self.business_domains[-1] if self.business_domains else "")

    def guess_tags(self, text: str) -> list[str]:
        tags = [tag for cue, tag in self.tag_cues.items() if cue in (text or "")]
        return tags or ["候选"]

    def prompt_context(self) -> str:
        """注入 LLM 提示词的领域说明书：领域名 + 子域划分 + 建模侧重。

        其中「子域线索」「标签线索」两行既提示真实模型，也被离线 Mock 解析，
        使不带密钥时产出的 business_domain / tags 同样跟随当前领域。
        """
        parts = [f"当前本体领域：{self.label}（领域包 {self.key}）。"]
        if self.agent_hint:
            parts.append(self.agent_hint)
        parts.append("business_domain 只能取以下之一：" + "、".join(self.business_domains) + "。")
        if self.domain_cues:
            parts.append(
                "子域线索："
                + "; ".join(f"{domain}={','.join(cues)}" for domain, cues in self.domain_cues.items())
                + "。"
            )
        if self.tag_cues:
            parts.append("标签线索：" + "; ".join(f"{cue}→{tag}" for cue, tag in self.tag_cues.items()) + "。")
        if self.term_suffixes:
            parts.append("术语后缀：" + "、".join(self.term_suffixes[:24]) + "。")
        if self.synonyms:
            parts.append(
                "同义口径：" + "；".join(f"{term}≈{'、'.join(values)}" for term, values in list(self.synonyms.items())[:10]) + "。"
            )
        if self.standards:
            parts.append("可引用的标准/规范：" + "、".join(self.standards[:6]) + "。")
        if self.facets:
            parts.append("建议补充的注解维度：" + "、".join(f"{k}（{v}）" for k, v in self.facets.items()) + "。")
        return "\n".join(parts)

    def as_descriptor(self) -> dict[str, Any]:
        """不含类型明细的轻量描述，供前端目录展示。"""
        return {
            "key": self.key,
            "label": self.label,
            "summary": self.summary,
            "space_name": self.space_name,
            "business_domains": self.business_domains,
            "kinds": {**self._base_kinds(), **self.extra_kinds},
            "facets": self.facets,
            "standards": self.standards,
            "classification": self.classification,
            "type_count": len(self.object_types),
            "link_count": len(self.links),
            "samples": [s.filename for s in self.samples],
            "sample_questions": self.sample_questions,
            "agent_hint": self.agent_hint,
        }

    def _base_kinds(self) -> dict[str, str]:
        from ..kinds import OBJECT_KINDS

        return dict(OBJECT_KINDS)
