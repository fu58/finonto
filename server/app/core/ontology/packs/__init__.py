"""领域包注册表：按本体空间解析所属领域，把领域词表、提示词上下文与安装动作交给服务层与前端。

新增领域只需要三步：在本目录写一个模块声明 `PACK = DomainPack(...)`，把示例语料放进
`server/data/samples/<key>/`，然后把它加进下面的 `PACKS`。服务层、接口与前端无需改动。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ....models import ObjectType, Space
from ..kinds import (
    CONSTRAINT_KINDS,
    OBJECT_KINDS,
    PROPERTY_DATATYPES,
    RELATION_CATEGORIES,
    SEVERITIES,
)
from . import ai, finance, general, maintenance, network
from .base import DomainPack, SampleDoc
from .install import get_or_create_space, ingest_pack_samples, install_pack, sample_manifest, seed_space
from ..service import OntologyService

PACKS: dict[str, DomainPack] = {
    pack.key: pack
    for pack in (finance.PACK, maintenance.PACK, ai.PACK, network.PACK, general.PACK)
}
# 空间 domain 认不出来时的回落：通用包不预设领域，适合尚未打包成领域包的新领域文档
DEFAULT_PACK_KEY = "general"


def all_packs() -> list[DomainPack]:
    return list(PACKS.values())


def get_pack(key: str | None) -> DomainPack | None:
    if not key:
        return None
    return PACKS.get(key)


def require_pack(key: str) -> DomainPack:
    pack = get_pack(key)
    if pack is None:
        raise KeyError(f"未知领域包 {key}，可用：{', '.join(PACKS)}")
    return pack


def pack_for_space(session: Session, space_id: int | None) -> DomainPack:
    """空间所属领域；自定义空间回落到默认领域包。"""
    if space_id:
        space = session.get(Space, space_id)
        if space and space.domain in PACKS:
            return PACKS[space.domain]
    return PACKS[DEFAULT_PACK_KEY]


def lexicon_for(session: Session, space_id: int | None) -> dict[str, Any]:
    """检索与生成侧使用的领域提示：领域标签 + 同义扩展表 + 术语后缀。"""
    pack = pack_for_space(session, space_id)
    return {"label": pack.label, "synonyms": pack.synonyms, "term_suffixes": pack.term_suffixes}


def observed_business_domains(session: Session, space_id: int | None, pack: DomainPack) -> list[str]:
    """领域包预置子域 + 该空间里人工/Agent 实际用到的子域，保证下拉框不丢选项。"""
    domains = list(pack.business_domains)
    if space_id:
        rows = session.execute(
            select(ObjectType.business_domain, func.count())
            .where(ObjectType.space_id == space_id)
            .group_by(ObjectType.business_domain)
        ).all()
        for value, _count in rows:
            if value and value not in domains:
                domains.append(value)
    return domains


def vocab_for(session: Session, space_id: int | None = None) -> dict[str, Any]:
    """空间感知的受控词表：通用内核词汇 + 所属领域包的扩展维度。"""
    pack = pack_for_space(session, space_id)
    return {
        "object_kinds": {**OBJECT_KINDS, **pack.extra_kinds},
        "datatypes": PROPERTY_DATATYPES,
        "relation_categories": {**RELATION_CATEGORIES, **pack.extra_relation_categories},
        "constraint_kinds": CONSTRAINT_KINDS,
        "severities": SEVERITIES,
        "classification": pack.classification,
        "standards": pack.standards,
        "facets": pack.facets,
        "business_domains": observed_business_domains(session, space_id, pack),
        "sample_questions": pack.sample_questions,
        "pack": {
            "key": pack.key,
            "label": pack.label,
            "summary": pack.summary,
            "space_name": pack.space_name,
            "agent_hint": pack.agent_hint,
        },
    }


def catalog(session: Session, server_root: Path) -> list[dict[str, Any]]:
    """领域包画廊：目录信息 + 是否已安装 + 空间规模 + 示例语料可用性。"""
    service = OntologyService(session)
    rows: list[dict[str, Any]] = []
    for pack in PACKS.values():
        space = session.scalar(select(Space).where(Space.name == pack.space_name))
        if space is None:
            space = session.scalar(select(Space).where(Space.domain == pack.key).order_by(Space.id))
        manifest = sample_manifest(pack, server_root)
        rows.append(
            {
                **pack.as_descriptor(),
                "installed": space is not None,
                "space": None
                if space is None
                else {
                    "id": space.id,
                    "name": space.name,
                    "domain": space.domain,
                    "iri": space.iri,
                    "stats": service.space_stats(space.id),
                },
                "sample_files": manifest,
                "samples_available": len(manifest),
            }
        )
    return rows


__all__ = [
    "DEFAULT_PACK_KEY",
    "DomainPack",
    "PACKS",
    "SampleDoc",
    "all_packs",
    "catalog",
    "get_or_create_space",
    "get_pack",
    "ingest_pack_samples",
    "install_pack",
    "lexicon_for",
    "pack_for_space",
    "require_pack",
    "sample_manifest",
    "seed_space",
    "vocab_for",
]
