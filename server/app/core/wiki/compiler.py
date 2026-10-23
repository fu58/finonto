"""Wiki 编译器：把本体空间投影成可维护的 Markdown 页面集。

- 每类页面都有固定 slug 规则，保证可重复编译；
- 内容哈希一致时不产生新版本，避免版本噪声；
- 人工编辑过的页面（locked_by_human）默认不被机器覆盖；
- 本体变更后只把相关页面标记 dirty，重编译时做增量处理。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import (
    ChangeProposal,
    Constraint,
    DataPropertyDef,
    DocumentRecord,
    Enumeration,
    Evidence,
    ObjectType,
    RelationType,
    Space,
    TypeDataProperty,
    TypeLink,
    WikiPage,
    WikiRevision,
)
from ..ontology.service import OntologyService

SLUG_TYPE = "type-{type_id}"
SLUG_INDEX = "index"
SLUG_DOMAIN = "domain-{slug}"
SLUG_RELATIONS = "relation-catalog"
SLUG_QUALITY = "quality-report"

AUTO_HEADER = (
    "<!-- 本页由 FinOnto 本体编译器生成。人工编辑会保留（页面被锁定后不再自动覆盖），"
    "所有版本可在「版本历史」中 diff 与回滚。 -->\n"
)


@dataclass
class CompileReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_locked: int = 0
    pages: list[dict[str, Any]] = field(default_factory=list)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]+", "-", (text or "").strip()).strip("-").lower()
    return slug or "domain"


def _digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def wiki_link(slug: str, label: str) -> str:
    return f"[[{slug}|{label}]]"


class WikiCompiler:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.ontology = OntologyService(session)

    # ------------------------------------------------------------ 页面获取
    def get_or_create_page(
        self, space_id: int, slug: str, *, title: str, page_type: str, object_type_id: int | None = None
    ) -> WikiPage:
        page = self.session.scalar(select(WikiPage).where(WikiPage.space_id == space_id, WikiPage.slug == slug))
        if page is None:
            page = WikiPage(
                space_id=space_id,
                slug=slug,
                title=title,
                page_type=page_type,
                object_type_id=object_type_id,
                current_version=0,
            )
            self.session.add(page)
            self.session.flush()
        return page

    def add_revision(
        self,
        page: WikiPage,
        body_md: str,
        *,
        author: str = "system",
        change_note: str = "",
        force: bool = False,
    ) -> WikiRevision | None:
        latest = self.session.scalar(
            select(WikiRevision)
            .where(WikiRevision.page_id == page.id)
            .order_by(WikiRevision.version_no.desc())
        )
        if latest and latest.body_md == body_md and not force:
            page.dirty = False
            return None
        if page.locked_by_human and author == "system" and not force:
            page.dirty = False
            return None
        revision = WikiRevision(
            page_id=page.id,
            version_no=(latest.version_no + 1) if latest else 1,
            body_md=body_md,
            summary=_digest(body_md),
            author=author,
            change_note=change_note,
        )
        self.session.add(revision)
        page.current_version = revision.version_no
        page.title = page.title or "未命名"
        page.dirty = False
        self.session.flush()
        return revision

    def current_body(self, page: WikiPage) -> str:
        revision = self.session.scalar(
            select(WikiRevision).where(WikiRevision.page_id == page.id).order_by(WikiRevision.version_no.desc())
        )
        return revision.body_md if revision else ""

    def current_revision(self, page: WikiPage) -> WikiRevision | None:
        return self.session.scalar(
            select(WikiRevision).where(WikiRevision.page_id == page.id).order_by(WikiRevision.version_no.desc())
        )

    # ------------------------------------------------------------ 编译
    def compile_space(self, space_id: int, *, only_dirty: bool = False, force: bool = False) -> CompileReport:
        space = self.session.get(Space, space_id)
        if space is None:
            raise KeyError(f"空间 {space_id} 不存在")
        report = CompileReport()
        types = list(self.session.scalars(select(ObjectType).where(ObjectType.space_id == space_id).order_by(ObjectType.id)))
        wanted: dict[str, tuple[str, str, int | None, str]] = {SLUG_INDEX: (self.render_index(space, types), "本体总览", None, "index")}
        for row in types:
            wanted[SLUG_TYPE.format(type_id=row.id)] = (
                self.render_type_page(space, row),
                row.label,
                row.id,
                "type",
            )
        for domain in sorted({t.business_domain or "通用" for t in types}):
            wanted[SLUG_DOMAIN.format(slug=_slugify(domain))] = (
                self.render_domain_page(space, domain, types),
                f"{domain} 领域枢纽",
                None,
                "domain",
            )
        wanted[SLUG_RELATIONS] = (self.render_relation_catalog(space_id), "关联语义目录", None, "relations")
        wanted[SLUG_QUALITY] = (self.render_quality_report(space_id), "本体质量报告", None, "quality")

        for slug, (body, title, type_id, page_type) in wanted.items():
            page = self.get_or_create_page(space_id, slug, title=title, page_type=page_type, object_type_id=type_id)
            if page.title != title:
                page.title = title
            if only_dirty and not page.dirty and self.current_body(page) and not force:
                continue
            if page.locked_by_human and not force:
                report.skipped_locked += 1
                report.pages.append({"slug": slug, "action": "skipped_locked", "title": title})
                continue
            revision = self.add_revision(page, body, author="system", change_note="本体编译")
            if revision is None:
                report.unchanged += 1
                report.pages.append({"slug": slug, "action": "unchanged", "title": title})
            elif revision.version_no == 1:
                report.created += 1
                report.pages.append({"slug": slug, "action": "created", "version": revision.version_no, "title": title})
            else:
                report.updated += 1
                report.pages.append({"slug": slug, "action": "updated", "version": revision.version_no, "title": title})
        self._prune_orphan_type_pages(space_id, {slug for slug in wanted if slug.startswith("type-")})
        self.session.flush()
        return report

    def _prune_orphan_type_pages(self, space_id: int, keep_slugs: set[str]) -> None:
        for page in self.session.scalars(
            select(WikiPage).where(WikiPage.space_id == space_id, WikiPage.page_type == "type")
        ).all():
            if page.slug in keep_slugs:
                continue
            self.session.delete(page)
        self.session.flush()

    # ------------------------------------------------------------ 渲染
    def render_index(self, space: Space, types: list[ObjectType]) -> str:
        stats = self.ontology.space_stats(space.id)
        lines = [
            AUTO_HEADER,
            f"# {space.name}",
            "",
            space.description or "",
            "",
            "## 规模概览",
            "",
            f"- 对象类型：**{stats['object_types']}**（生效 {stats['active_types']} / 草稿 {stats['draft_types']} / 弃用 {stats['deprecated_types']}）",
            f"- 数据属性定义：**{stats['data_properties']}** · 关联语义：**{stats['relation_types']}** · 类型间关联：**{stats['links']}**",
            f"- 约束：**{stats['constraints']}** · 枚举：**{stats['enumerations']}** · 待确认提案：**{stats['pending_proposals']}**",
            "",
            "## 业务域分布",
            "",
            "| 业务域 | 对象类型数 | 入口 |",
            "| --- | --- | --- |",
        ]
        for domain, count in sorted(stats["by_domain"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {domain or '通用'} | {count} | {wiki_link(SLUG_DOMAIN.format(slug=_slugify(domain)), '进入')} |")
        lines += ["", "## 对象类型索引", "", "| 对象类型 | 英文名 | 类别 | 状态 | 置信度 | 来源 |", "| --- | --- | --- | --- | --- | --- |"]
        for row in sorted(types, key=lambda t: (t.business_domain or "通用", t.label)):
            lines.append(
                f"| {wiki_link(SLUG_TYPE.format(type_id=row.id), row.label)} | {row.label_en or '-'} | "
                f"{row.kind} | {row.status} | {round(row.confidence, 2)} | {row.origin} |"
            )
        lines += [
            "",
            "## 其他入口",
            "",
            f"- {wiki_link(SLUG_RELATIONS, '关联语义目录')}",
            f"- {wiki_link(SLUG_QUALITY, '本体质量报告')}",
            "",
            f"> 最近编译：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        ]
        return "\n".join(lines)

    def render_type_page(self, space: Space, row: ObjectType) -> str:
        detail = self.ontology.detail(row.id)
        parent = detail["parent"]
        lines = [AUTO_HEADER, f"# {row.label}", ""]
        if row.label_en or row.plural_label:
            lines.append(f"> 英文名：`{row.label_en or '-'}` · 复数：{row.plural_label or '-'} · IRI：`{row.iri}`")
            lines.append("")
        lines += ["## 定义", "", row.definition or "_（尚未补充定义，可在「本体工作室」编辑或让 Agent 生成提案）_", ""]
        if row.aliases:
            lines += ["## 别名", "", "、".join(f"`{a}`" for a in row.aliases), ""]
        lines += ["## 元信息", "", f"- 类别：`{row.kind}` · 抽象：{'是' if row.abstract else '否'} · 状态：`{row.status}`", f"- 业务域：{row.business_domain} · 来源：{row.origin} · 置信度：{round(row.confidence, 2)} · 版本：v{row.version_no}", ""]
        lines += ["## 分类位置", ""]
        if parent:
            lines.append(f"- 上位：{wiki_link(SLUG_TYPE.format(type_id=parent['id']), parent['label'])}")
        else:
            lines.append("- 上位：无（顶层概念）")
        children = detail["children"]
        if children:
            lines.append("- 下位：" + "、".join(wiki_link(SLUG_TYPE.format(type_id=c['id']), c["label"]) for c in children))
        lines.append("")

        lines += ["## 属性", ""]
        if detail["properties"]:
            lines += ["| 属性 | IRI 局部名 | 数据类型 | 单位 | 必填 | 唯一 | 基数 | 说明 |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
            for prop in detail["properties"]:
                local = prop["iri"].rsplit("/", 1)[-1] if prop["iri"] else "-"
                enum_note = ("；可选值：" + "/".join(map(str, prop["enum"]))) if prop["enum"] else ""
                lines.append(
                    f"| {prop['label']} | `{local}` | `{prop['datatype']}` | {prop['unit'] or '-'} | "
                    f"{'✅' if prop['required'] else '—'} | {'✅' if prop['unique'] else '—'} | "
                    f"{prop['min_card']}..{prop['max_card']} | {(prop['description'] or '')[:80]}{enum_note} |"
                )
        else:
            lines.append("_尚未定义数据属性。_")
        lines.append("")

        lines += ["## 关联", ""]
        if detail["links"]:
            lines += ["| 方向 | 关系 | 目标对象类型 | 基数 | 类别 | 置信度 | 说明 |", "| --- | --- | --- | --- | --- | --- | --- |"]
            for link in detail["links"]:
                arrow = "→" if link["direction"] == "out" else "←"
                other = f"**{link['target_label']}**"
                lines.append(
                    f"| {arrow} | {link['relation']} | {wiki_link(SLUG_TYPE.format(type_id=link['target_id']), other.strip('*'))} | "
                    f"{link['cardinality']} | {link['category']} | {round(link['confidence'], 2)} | {(link['description'] or '')[:70]} |"
                )
        else:
            lines.append("_尚未建立类型间关联。_")
        lines.append("")

        lines += ["## 约束", ""]
        if detail["constraints"]:
            for con in detail["constraints"]:
                lines.append(f"- **{con['name']}**（{con['kind']} / {con['severity']}）：{con['expression'] or con['description']}")
                if con["reg_ref"]:
                    lines.append(f"  - 依据：{con['reg_ref']}")
        else:
            lines.append("_无显式约束。_")
        lines.append("")

        ann = detail["annotations"] or {}
        if ann:
            lines += ["## 领域注解", ""]
            for key, value in ann.items():
                rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                lines.append(f"- **{key}**：{rendered}")
            lines.append("")

        lines += ["## 证据链", ""]
        evidence = detail["evidence"] or []
        if evidence:
            for ev in evidence[:12]:
                if not ev.get("quote"):
                    continue
                lines.append(f"- 「{ev['quote'][:160]}」 — {ev['doc_name'] or '文档'} {ev['locator']}（#{ev['proposal_id'] or '-'}）")
        else:
            lines.append("_该类型目前无原文证据引用。_")
        lines.append("")

        related_proposals = self._proposals_for(space.id, row.label)
        if related_proposals:
            lines += ["## 相关待确认提案", ""] + [f"- #{p.id} {p.title}（置信度 {round(p.confidence, 2)}）" for p in related_proposals] + [""]
        lines.append(f"_数据更新时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC · 页面标识：`{row.iri}`_")
        return "\n".join(lines)

    def render_domain_page(self, space: Space, domain: str, all_types: list[ObjectType]) -> str:
        types = [t for t in all_types if (t.business_domain or "通用") == domain]
        lines = [AUTO_HEADER, f"# {domain} 领域枢纽", "", f"本域共 **{len(types)}** 个对象类型。", "", "## 对象类型", ""]
        for row in sorted(types, key=lambda t: t.label):
            lines.append(f"### {wiki_link(SLUG_TYPE.format(type_id=row.id), row.label)}")
            lines.append("")
            lines.append(f"{row.definition or '_（缺定义）_'}")
            lines.append("")
        links = self.session.scalars(select(TypeLink).where(TypeLink.space_id == space.id)).all()
        ids = {t.id for t in types}
        internal = [l for l in links if l.source_id in ids and l.target_id in ids]
        external = [l for l in links if (l.source_id in ids) != (l.target_id in ids)]
        lines += ["## 域内关联", ""]
        if internal:
            for link in internal:
                source = self.session.get(ObjectType, link.source_id)
                target = self.session.get(ObjectType, link.target_id)
                rel = self.session.get(RelationType, link.relation_id)
                lines.append(
                    f"- {wiki_link(SLUG_TYPE.format(type_id=source.id), source.label)} "
                    f"— {rel.label if rel else '关联'} → "
                    f"{wiki_link(SLUG_TYPE.format(type_id=target.id), target.label)}"
                )
        else:
            lines.append("_域内暂无已断言关联。_")
        if external:
            lines += ["", "## 跨域关联", ""]
            for link in external:
                source = self.session.get(ObjectType, link.source_id)
                target = self.session.get(ObjectType, link.target_id)
                rel = self.session.get(RelationType, link.relation_id)
                lines.append(f"- {source.label} — {rel.label if rel else '关联'} → {target.label}")
        return "\n".join(lines)

    def render_relation_catalog(self, space_id: int) -> str:
        rels = self.session.scalars(select(RelationType).where(RelationType.space_id == space_id)).all()
        lines = [AUTO_HEADER, "# 关联语义目录", "", "| 关系 | 英文名 | 类别 | 逆关系 | 对称 | 传递 | 功能 | 断言数 | 说明 |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for rel in sorted(rels, key=lambda r: (r.category, r.label)):
            count = len(self.session.scalars(select(TypeLink).where(TypeLink.relation_id == rel.id)).all())
            lines.append(
                f"| {rel.label} | `{rel.label_en}` | {rel.category} | `{rel.inverse_iri or '-'}` | "
                f"{'是' if rel.symmetric else '否'} | {'是' if rel.transitive else '否'} | {'是' if rel.functional else '否'} | "
                f"{count} | {(rel.description or '')[:70]} |"
            )
        return "\n".join(lines)

    def render_quality_report(self, space_id: int) -> str:
        issues = self.ontology.validate_space(space_id)
        stats = self.ontology.space_stats(space_id)
        lines = [
            AUTO_HEADER,
            "# 本体质量报告",
            "",
            f"- 问题总数：**{len(issues)}**（阻断 {sum(1 for i in issues if i.level == 'violation')} / "
            f"告警 {sum(1 for i in issues if i.level == 'warning')} / 提示 {sum(1 for i in issues if i.level == 'info')}）",
            f"- 定义完整率：**{round(100 * max(0, stats['object_types'] - sum(1 for i in issues if i.code == 'definition_missing')) / max(stats['object_types'], 1), 1)}%**",
            "",
            "| 级别 | 代码 | 说明 | 定位 |",
            "| --- | --- | --- | --- |",
        ]
        for issue in issues[:120]:
            lines.append(f"| {issue.level} | `{issue.code}` | {issue.message} | `{issue.path}` |")
        if not issues:
            lines.append("| — | — | 未发现问题 | — |")
        return "\n".join(lines)

    def _proposals_for(self, space_id: int, label: str) -> list[ChangeProposal]:
        rows = self.session.scalars(
            select(ChangeProposal).where(ChangeProposal.space_id == space_id, ChangeProposal.status == "pending")
        ).all()
        return [p for p in rows if (p.payload or {}).get("label") == label or (p.payload or {}).get("subject") == label]

    # ------------------------------------------------------------ 页面操作
    def list_pages(self, space_id: int) -> list[WikiPage]:
        return list(self.session.scalars(select(WikiPage).where(WikiPage.space_id == space_id).order_by(WikiPage.slug)).all())

    def page_view(self, page: WikiPage) -> dict[str, Any]:
        revision = self.current_revision(page)
        return {
            "id": page.id,
            "slug": page.slug,
            "title": page.title,
            "page_type": page.page_type,
            "object_type_id": page.object_type_id,
            "current_version": page.current_version,
            "locked_by_human": page.locked_by_human,
            "dirty": page.dirty,
            "body_md": revision.body_md if revision else "",
            "author": revision.author if revision else "",
            "change_note": revision.change_note if revision else "",
            "updated_at": page.updated_at.isoformat() if page.updated_at else "",
            "revision_count": len(page.revisions),
        }

    def manual_edit(self, page_id: int, body_md: str, *, note: str = "", author: str = "human", lock: bool = True) -> dict[str, Any]:
        page = self.session.get(WikiPage, page_id)
        if page is None:
            raise KeyError(str(page_id))
        revision = self.add_revision(page, body_md, author=author, change_note=note or "人工编辑", force=True)
        if revision is None:
            return {"page": self.page_view(page), "revision": None, "message": "内容与当前版本一致，未生成新版本"}
        if lock:
            page.locked_by_human = True
        self.session.flush()
        return {"page": self.page_view(page), "revision": _revision_view(revision), "message": f"已保存为 v{revision.version_no}"}

    def set_lock(self, page_id: int, locked: bool) -> dict[str, Any]:
        page = self.session.get(WikiPage, page_id)
        if page is None:
            raise KeyError(str(page_id))
        page.locked_by_human = locked
        if locked:
            page.dirty = False
        self.session.flush()
        return self.page_view(page)


def _revision_view(revision: WikiRevision) -> dict[str, Any]:
    return {
        "id": revision.id,
        "version_no": revision.version_no,
        "author": revision.author,
        "change_note": revision.change_note,
        "created_at": revision.created_at.isoformat() if revision.created_at else "",
        "digest": revision.summary,
        "chars": len(revision.body_md or ""),
    }
