"""领域包注册表——**新增领域唯一需要改的文件**。

加一个领域：
    1. 本目录新建 ``medical.py``，声明 ``PACK = DomainPack(...)``；
    2. 语料放 ``server/data/samples/medical/``；
    3. 在下面的 ``_MODULES`` / ``PACKS`` 里加一行。
服务层、接口、模板、前端一行都不用改。
"""

from __future__ import annotations

from typing import Iterable

from . import ai, finance, general, maintenance, network
from .base import (
    DEFAULT_KINDS,
    DEFAULT_RELATION_CATEGORIES,
    SAMPLES_ROOT,
    DomainPack,
    SampleDoc,
)

#: 内置领域包（画廊顺序）
PACKS: dict[str, DomainPack] = {
    pack.key: pack
    for pack in (finance.PACK, maintenance.PACK, ai.PACK, network.PACK, general.PACK)
}

#: 认不出 domain 时的回落
DEFAULT_PACK_KEY = "general"


def get_pack(key: str | None) -> DomainPack:
    """永远返回一个有效的领域包——未知 key 回落到通用域，而不是抛错。"""
    if key and key in PACKS:
        return PACKS[key]
    return PACKS[DEFAULT_PACK_KEY]


def pack_exists(key: str | None) -> bool:
    return bool(key) and key in PACKS


def all_packs() -> list[DomainPack]:
    return list(PACKS.values())


def real_packs() -> list[DomainPack]:
    """排除空白通用域的真领域包。"""
    return [pack for pack in PACKS.values() if pack.key != DEFAULT_PACK_KEY]


def vocab_for(domain: str | None) -> dict:
    """给前端的受控词表（下拉框、筛选器）。"""
    return get_pack(domain).vocab()


def lexicon_for(domain: str | None) -> dict:
    """给查询理解的同义词扩展与术语后缀。"""
    pack = get_pack(domain)
    return {
        "synonyms": pack.synonyms,
        "term_suffixes": pack.term_suffixes,
        "domain_cues": pack.domain_cues,
        "clause_markers": pack.clause_markers,
    }


def prompt_context_for(domain: str | None) -> str:
    return get_pack(domain).prompt_context()


def sample_questions_for(domain: str | None) -> list[str]:
    return get_pack(domain).sample_questions


def gallery() -> list[dict]:
    """领域包画廊：前端卡片直接渲染这个结构。"""
    items: list[dict] = []
    for pack in PACKS.values():
        manifest = pack.sample_manifest()
        items.append(
            {
                "key": pack.key,
                "label": pack.label,
                "summary": pack.summary,
                "space_name": pack.space_name,
                "space_iri": pack.space_iri,
                "type_count": len(pack.object_types),
                "link_count": len(pack.links),
                "business_domains": pack.business_domains,
                "standards": pack.standards,
                "classification": pack.classification,
                "facets": pack.facets,
                "samples": manifest,
                "samples_available": sum(1 for item in manifest if item["available"]),
                "samples_root": str(SAMPLES_ROOT),
                "sample_questions": pack.sample_questions,
            }
        )
    return items


def merged_samples(keys: Iterable[str]) -> list[SampleDoc]:
    out: list[SampleDoc] = []
    for key in keys:
        out.extend(get_pack(key).samples)
    return out


__all__ = [
    "PACKS",
    "DEFAULT_PACK_KEY",
    "DEFAULT_KINDS",
    "DEFAULT_RELATION_CATEGORIES",
    "DomainPack",
    "SampleDoc",
    "SAMPLES_ROOT",
    "get_pack",
    "pack_exists",
    "all_packs",
    "real_packs",
    "vocab_for",
    "lexicon_for",
    "prompt_context_for",
    "sample_questions_for",
    "gallery",
    "merged_samples",
]
