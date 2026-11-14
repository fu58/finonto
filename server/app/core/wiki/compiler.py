"""Wiki 编译器：把本体投影成可编辑、可版本化的 Markdown 页面。

五类页面
--------
* ``index``       —— 空间概览与导航
* ``type-<id>``   —— 每个对象类型一页（定义/属性/关联/约束/证据链）
* ``domain-<x>``  —— 每个业务域一页
* ``relations``   —— 关联语义目录（按语义分组，带使用次数）
* ``quality``     —— 本次编译时的校验摘要

增量编译：只有 ``is_dirty`` 的页面会被重写（提案确认后由服务层置脏），
``is_locked`` 的人工锁定页永远跳过。每次写入都会追加一条 ``WikiRevision``，
所以「编译」和「人工编辑」共享同一条版本链。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select

from ...models import (
    Constraint,
    DataPropertyDef,
    Evidence,
    ObjectType,
    OntologySpace,
    RelationType,
    TypeDataProperty,
    TypeLink,
    WikiPage,
    WikiRevision,
)
from ..ontology import kinds, packs
from ..text import slugify, truncate

PAGE_TYPES = ("index", "type", "domain", "relations", "quality")


def digest_of(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 页面构造
# --------------------------------------------------------------------------- #
def _property_rows(session, type_id: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(DataPropertyDef, TypeDataProperty)
        .join(TypeDataProperty, TypeDataProperty.property_id == DataPropertyDef.id)
        .where(TypeDataProperty.type_id == type_id)
        .order_by(TypeDataProperty.ordinal)
    ).all()
    return [
        {
            "label": prop.label,
            "key": prop.key,
            "data_type": kinds.PROPERTY_TYPE_LABELS.get(prop.data_type, prop.data_type),
            "unit": prop.unit,
            "required": bool(link.is_required or prop.is_required),
            "enum_values": prop.enum_values or [],
        }
        for prop, link in rows
    ]


def _link_rows(session, type_id: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(TypeLink, ObjectType, RelationType)
        .join(ObjectType, ObjectType.id == TypeLink.target_type_id)
        .join(RelationType, RelationType.id == TypeLink.relation_id)
        .where(TypeLink.source_type_id == type_id)
    ).all()
    return [
        {
            "relation": relation.label,
            "category": kinds.relation_label(relation.category),
            "target_id": target.id,
            "target_label": target.label,
            "cardinality": link.cardinality,
        }
        for link, target, relation in rows
    ]


def _constraint_rows(session, type_id: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Constraint, DataPropertyDef)
        .outerjoin(DataPropertyDef, DataPropertyDef.id == Constraint.property_id)
        .where(Constraint.type_id == type_id, Constraint.is_active.is_(True))
    ).all()
    return [
        {
            "kind": kinds.CONSTRAINT_KIND_LABELS.get(item.kind, item.kind),
            "severity": kinds.severity_label(item.severity),
            "expression": item.expression,
            "message": item.message,
            "property": prop.label if prop else "",
            "regulation_ref": item.regulation_ref,
        }
        for item, prop in rows
    ]


def _evidence_rows(session, item: ObjectType) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Evidence).where(
            Evidence.space_id == item.space_id, Evidence.note == item.label
        )
    ).scalars().all()
    return [
        {"locator": item.locator, "quote": truncate(item.quote, 160)} for item in rows[:5]
    ]


def build_type_page(session, item: ObjectType) -> str:
    lines: list[str] = [f"# {item.label}", ""]
    meta = [
        f"**种类**：{kinds.kind_label(item.kind)}",
        f"**业务域**：{item.business_domain or '未分域'}",
        f"**置信度**：{item.confidence:.2f}",
        f"**来源**：{item.source}",
        f"**版本**：v{item.version}",
        f"**IRI**：`{item.iri}`",
    ]
    lines.append(" ｜ ".join(meta))
    lines.append("")
    lines.append(item.definition or "> 尚未填写定义。")
    lines.append("")

    if item.aliases:
        lines.append(f"**别名**：{'、'.join(item.aliases)}")
        lines.append("")

    if item.parent_id:
        parent = session.get(ObjectType, item.parent_id)
        if parent is not None:
            lines.append(f"**上级类型**：[[type-{parent.id}]] {parent.label}")
            lines.append("")

    props = _property_rows(session, item.id)
    lines.append("## 数据属性")
    if props:
        lines.append("| 属性 | 类型 | 单位 | 必填 | 枚举 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for prop in props:
            enums = "、".join(prop["enum_values"]) if prop["enum_values"] else "—"
            lines.append(
                f"| {prop['label']} | {prop['data_type']} | {prop['unit'] or '—'} "
                f"| {'是' if prop['required'] else '否'} | {enums} |"
            )
    else:
        lines.append("_该类型尚未定义数据属性。_")
    lines.append("")

    links = _link_rows(session, item.id)
    lines.append("## 关联")
    if links:
        for link in links:
            target = f"[[type-{link['target_id']}]] {link['target_label']}"
            lines.append(f"- `{link['relation']}`（{link['category']}）→ {target} ｜ 基数 {link['cardinality']}")
    else:
        lines.append("_暂无出边关联。_")
    lines.append("")

    constraints = _constraint_rows(session, item.id)
    lines.append("## 约束（SHACL 风格）")
    if constraints:
        for constraint in constraints:
            ref = f"（依据：{constraint['regulation_ref']}）" if constraint["regulation_ref"] else ""
            lines.append(
                f"- **{constraint['kind']}** / {constraint['severity']}：{constraint['message']} "
                f"`{constraint['expression']}`{ref}"
            )
    else:
        lines.append("_暂无约束。_")
    lines.append("")

    evidence = _evidence_rows(session, item)
    lines.append("## 证据链")
    if evidence:
        for record in evidence:
            lines.append(f"- {record['locator']}：「{record['quote']}」")
    else:
        lines.append("_尚未关联证据，建议在提案确认时补充来源引文。_")
    lines.append("")
    lines.append("---")
    lines.append(f"_本页由 Wiki 编译器生成；最后更新于 v{item.version}。_")
    return "\n".join(lines)


def build_index_page(session, space: OntologySpace) -> str:
    types = session.execute(
        select(ObjectType).where(ObjectType.space_id == space.id).order_by(ObjectType.label)
    ).scalars().all()
    domains: dict[str, list[ObjectType]] = {}
    for item in types:
        domains.setdefault(item.business_domain or "未分域", []).append(item)
    pack = packs.get_pack(space.domain)

    lines = [
        f"# {space.name}",
        "",
        space.description or "（无描述）",
        "",
        f"- 领域包：**{pack.label}**（`{pack.key}`）",
        f"- 空间 IRI：`{space.iri}`",
        f"- 对象类型：**{len(types)}** 个 ｜ 业务域：**{len(domains)}** 个",
        "",
        "## 业务域导航",
        "",
    ]
    for domain, items in sorted(domains.items(), key=lambda pair: -len(pair[1])):
        lines.append(f"- [[domain-{slugify(domain)}]] **{domain}**（{len(items)} 个类型）")
    lines.extend(["", "## 对象类型", "", "| 类型 | 种类 | 业务域 | 置信度 |", "| --- | --- | --- | --- |"])
    for item in types:
        lines.append(
            f"| [[type-{item.id}]] {item.label} | {kinds.kind_label(item.kind)} "
            f"| {item.business_domain or '未分域'} | {item.confidence:.2f} |"
        )
    lines.extend(
        [
            "",
            "## 其他页面",
            "",
            "- [[relations]] 关联语义目录",
            "- [[quality]] 本体质量报告",
            "",
            f"_编译器生成 · 空间 #{space.id}_",
        ]
    )
    return "\n".join(lines)


def build_domain_page(session, space: OntologySpace, domain: str, items: list[ObjectType]) -> str:
    lines = [f"# {domain}", "", f"业务域页 · 空间《{space.name}》 · 共 {len(items)} 个对象类型", ""]
    for item in sorted(items, key=lambda x: x.label):
        lines.append(f"## [[type-{item.id}]] {item.label}")
        lines.append("")
        lines.append(item.definition or "_（无定义）_")
        lines.append("")
        props = _property_rows(session, item.id)
        if props:
            lines.append("关键属性：" + "、".join(prop["label"] for prop in props[:5]))
            lines.append("")
    return "\n".join(lines)


def build_relations_page(session, space: OntologySpace) -> str:
    relations = session.execute(
        select(RelationType).where(RelationType.space_id == space.id)
    ).scalars().all()
    lines = [f"# 关联语义目录 · {space.name}", "", "按语义分组列出空间内全部关联类型及其使用次数。", ""]
    by_category: dict[str, list[RelationType]] = {}
    for relation in relations:
        by_category.setdefault(relation.category, []).append(relation)
    for category, items in sorted(by_category.items()):
        lines.append(f"## {kinds.relation_label(category)}")
        lines.append("")
        lines.append("| 关联 | 说明 | 使用次数 |")
        lines.append("| --- | --- | --- |")
        for relation in sorted(items, key=lambda x: x.label):
            count = len(
                session.execute(
                    select(TypeLink.id).where(TypeLink.relation_id == relation.id)
                ).all()
            )
            lines.append(f"| `{relation.label}` | {relation.description or '—'} | {count} |")
        lines.append("")
    return "\n".join(lines)


def build_quality_page(session, space: OntologySpace) -> str:
    from ..ontology import service

    report = service.validate_space(session, space.id)
    lines = [
        f"# 本体质量报告 · {space.name}",
        "",
        f"违规 **{len(report['violation'])}** ｜ 警告 **{len(report['warning'])}** ｜ 提示 **{len(report['info'])}**",
        "",
    ]
    for level in ("violation", "warning", "info"):
        issues = report.get(level) or []
        if not issues:
            continue
        lines.append(f"## {kinds.severity_label(level)}（{len(issues)}）")
        lines.append("")
        for issue in issues:
            lines.append(f"- **{issue['type_label']}**：{issue['message']}")
        lines.append("")
    if not any(report.get(level) for level in ("violation", "warning", "info")):
        lines.append("本次校验未发现问题。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 编译主流程
# --------------------------------------------------------------------------- #
def _upsert_page(
    session,
    space: OntologySpace,
    *,
    slug: str,
    page_type: str,
    title: str,
    body: str,
    author: str = "compiler",
    message: str = "编译",
    only_dirty: bool = False,
) -> str:
    page = session.execute(
        select(WikiPage).where(WikiPage.space_id == space.id, WikiPage.slug == slug)
    ).scalars().first()

    if page is not None and page.is_locked:
        return "locked"
    if page is not None and only_dirty and not page.is_dirty:
        return "skipped"

    digest = digest_of(body)
    if page is None:
        page = WikiPage(
            space_id=space.id,
            slug=slug,
            page_type=page_type,
            title=title,
            body=body,
            version=1,
            is_dirty=False,
        )
        session.add(page)
        session.flush()
        session.add(
            WikiRevision(
                page_id=page.id,
                version=1,
                title=title,
                body=body,
                digest=digest,
                author=author,
                message=message,
            )
        )
        return "created"

    if page.body == body and page.title == title:
        page.is_dirty = False
        return "unchanged"

    page.version += 1
    page.title = title
    page.body = body
    page.is_dirty = False
    session.add(
        WikiRevision(
            page_id=page.id,
            version=page.version,
            title=title,
            body=body,
            digest=digest,
            author=author,
            message=message,
        )
    )
    return "updated"


def compile_space(
    session,
    space_id: int,
    *,
    only_dirty: bool = False,
    author: str = "compiler",
    message: str = "编译",
) -> dict:
    space = session.get(OntologySpace, space_id)
    if space is None:
        return {"status": "missing", "space_id": space_id}

    types = session.execute(
        select(ObjectType).where(ObjectType.space_id == space_id).order_by(ObjectType.label)
    ).scalars().all()

    stats = {"created": 0, "updated": 0, "skipped": 0, "locked": 0, "unchanged": 0, "pages": 0}

    def record(result: str) -> None:
        stats[result] = stats.get(result, 0) + 1
        if result in ("created", "updated"):
            stats["pages"] += 1

    for item in types:
        record(
            _upsert_page(
                session,
                space,
                slug=f"type-{item.id}",
                page_type="type",
                title=item.label,
                body=build_type_page(session, item),
                author=author,
                message=message,
                only_dirty=only_dirty,
            )
        )

    domains: dict[str, list[ObjectType]] = {}
    for item in types:
        domains.setdefault(item.business_domain or "未分域", []).append(item)
    for domain, items in domains.items():
        record(
            _upsert_page(
                session,
                space,
                slug=f"domain-{slugify(domain)}",
                page_type="domain",
                title=domain,
                body=build_domain_page(session, space, domain, items),
                author=author,
                message=message,
                only_dirty=only_dirty,
            )
        )

    record(
        _upsert_page(
            session,
            space,
            slug="relations",
            page_type="relations",
            title="关联语义目录",
            body=build_relations_page(session, space),
            author=author,
            message=message,
            only_dirty=only_dirty,
        )
    )
    record(
        _upsert_page(
            session,
            space,
            slug="quality",
            page_type="quality",
            title="本体质量报告",
            body=build_quality_page(session, space),
            author=author,
            message=message,
            only_dirty=only_dirty,
        )
    )
    record(
        _upsert_page(
            session,
            space,
            slug="index",
            page_type="index",
            title=space.name,
            body=build_index_page(session, space),
            author=author,
            message=message,
            only_dirty=only_dirty,
        )
    )

    session.commit()
    stats["status"] = "compiled"
    stats["space_id"] = space_id
    stats["type_pages"] = len(types)
    return stats


def mark_dirty(session, space_id: int, slugs: list[str] | None = None) -> int:
    """把页面置为待重编译；不传 slugs 表示整空间。"""
    query = select(WikiPage).where(WikiPage.space_id == space_id)
    if slugs:
        query = query.where(WikiPage.slug.in_(slugs))
    pages = session.execute(query).scalars().all()
    for page in pages:
        page.is_dirty = True
    return len(pages)


def type_slug(type_id: int) -> str:
    return f"type-{type_id}"


def page_links(body: str) -> list[str]:
    return re.findall(r"\[\[([a-zA-Z0-9\-_]+)\]\]", body or "")
