"""领域包（Domain Pack）契约。

一个领域包 = 一个 Python 模块，声明「这个领域里有什么」，不含任何逻辑分支。
新增领域只需要：写一个 ``xxx.py`` 声明 ``PACK``，放进 ``server/data/samples/<key>/`` 语料，
在 ``packs/__init__.py`` 的 ``PACKS`` 加一行。服务层、接口层、前端一行都不用改。

为什么领域不能写死在代码里
--------------------------
写死的话，每加一个领域就要改「词表」「提示词」「前端下拉框」「导出模板」四处，
改一处漏一处，最后变成 if/elif 大杂烩。这里把领域知识全部收敛成**数据**，
代码只消费数据，所以「加领域」= 加数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SAMPLES_ROOT = Path(__file__).resolve().parents[4] / "data" / "samples"


@dataclass(slots=True)
class SampleDoc:
    """随领域包发布的示例语料描述（文件在 ``data/samples/<pack>/<filename>``）。"""

    filename: str
    title: str
    doc_type: str = "制度"
    issuer: str = ""
    jurisdiction: str = ""
    authority: int = 3
    language: str = "zh"

    def path(self, pack_key: str) -> Path:
        return SAMPLES_ROOT / pack_key / self.filename

    def exists(self, pack_key: str) -> bool:
        return self.path(pack_key).is_file()


@dataclass(slots=True)
class DomainPack:
    # ---- 身份 ----
    key: str
    label: str
    summary: str
    space_name: str
    space_iri: str
    space_description: str

    # ---- 受控词表 ----
    business_domains: list[str] = field(default_factory=list)
    kinds: tuple[str, ...] = ()
    relation_categories: tuple[str, ...] = ()

    # ---- 基线本体 ----
    object_types: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)

    # ---- 示例语料 ----
    samples: list[SampleDoc] = field(default_factory=list)

    # ---- 离线抽取线索（无模型时决定「什么算术语」）----
    term_suffixes: tuple[str, ...] = ()
    clause_markers: tuple[str, ...] = ()
    domain_cues: tuple[str, ...] = ()
    tag_cues: tuple[str, ...] = ()
    synonyms: dict[str, list[str]] = field(default_factory=dict)

    # ---- 标准与分类（导出 SKOS / 报告用）----
    standards: list[str] = field(default_factory=list)
    classification: list[str] = field(default_factory=list)
    facets: list[str] = field(default_factory=list)

    # ---- 交互 ----
    sample_questions: list[str] = field(default_factory=list)
    agent_hint: str = ""

    # ------------------------------------------------------------------ #
    def type_labels(self) -> list[str]:
        return [item["label"] for item in self.object_types if item.get("label")]

    def vocab(self) -> dict[str, Any]:
        """给前端受控词表（下拉框/筛选器直接消费）。"""
        return {
            "pack": self.key,
            "label": self.label,
            "business_domains": self.business_domains,
            "kinds": list(self.kinds or DEFAULT_KINDS),
            "relation_categories": list(self.relation_categories or DEFAULT_RELATION_CATEGORIES),
            "standards": self.standards,
            "classification": self.classification,
            "facets": self.facets,
            "type_count": len(self.object_types),
            "link_count": len(self.links),
        }

    def prompt_context(self) -> str:
        """注入草案服务与 ReAct 系统提示的领域上下文。"""
        lines = [
            f"领域：{self.label}（{self.key}）",
            f"业务子域：{'、'.join(self.business_domains) or '未限定'}",
            f"术语后缀线索：{'、'.join(self.term_suffixes) or '无'}",
            f"条款标记：{'、'.join(self.clause_markers) or '无'}",
            f"领域线索词：{'、'.join(self.domain_cues) or '无'}",
        ]
        if self.standards:
            lines.append(f"参考标准：{'、'.join(self.standards)}")
        if self.agent_hint:
            lines.append(f"推理提示：{self.agent_hint}")
        return "\n".join(lines)

    def sample_manifest(self) -> list[dict[str, Any]]:
        return [
            {
                "filename": doc.filename,
                "title": doc.title,
                "doc_type": doc.doc_type,
                "authority": doc.authority,
                "available": doc.exists(self.key),
                "size": doc.path(self.key).stat().st_size if doc.exists(self.key) else 0,
            }
            for doc in self.samples
        ]


#: 全领域通用的对象类型种类（领域包可以追加，但不该收窄）
DEFAULT_KINDS: tuple[str, ...] = (
    "class",
    "agreement",
    "role",
    "document",
    "metric",
    "event",
    "organization",
    "asset",
    "process",
)

#: 全领域通用的关联语义
DEFAULT_RELATION_CATEGORIES: tuple[str, ...] = (
    "taxonomy",
    "participant",
    "reference",
    "derivation",
    "regulatory",
    "part_of",
    "causal",
)
