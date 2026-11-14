"""本体服务：空间、领域包安装、提案、唯一 IRI、校验、相似类型。

**人工确认闸门在这里**：``accept_proposal`` 是全项目唯一能改变本体结构
（并触发 Wiki 增量重编译）的函数。RAG、Agent、脚本都只能产出提案；
UI 层不做权限判断——企业里 UI 永远会被绕过（curl、定时任务、批处理）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from ...models import utcnow
from ...models import (
    ChangeProposal,
    Constraint,
    DataPropertyDef,
    Evidence,
    ObjectType,
    OntologySpace,
    RelationType,
    TypeDataProperty,
    TypeLink,
    WikiPage,
)
from ...providers.base import Message
from ..rag.ingest import ingest_bytes
from ..text import overlap_ratio, slugify, truncate
from . import kinds, packs

#: 提案里允许出现的动作
PROPOSAL_ACTIONS = ("create_type", "update_type", "create_link", "create_constraint")


# --------------------------------------------------------------------------- #
# 空间
# --------------------------------------------------------------------------- #
def ensure_space(session, pack_key: str) -> OntologySpace:
    """幂等：已存在则返回，不存在则按领域包创建。"""
    pack = packs.get_pack(pack_key)
    space = session.execute(
        select(OntologySpace).where(OntologySpace.key == pack.key)
    ).scalars().first()
    if space is not None:
        return space
    space = OntologySpace(
        key=pack.key,
        name=pack.space_name,
        iri=pack.space_iri,
        description=pack.space_description,
        domain=pack.key,
        status="active",
    )
    session.add(space)
    session.commit()
    return space


def list_spaces(session) -> list[dict[str, Any]]:
    spaces = session.execute(select(OntologySpace).order_by(OntologySpace.id)).scalars().all()
    result: list[dict[str, Any]] = []
    for space in spaces:
        type_count = session.execute(
            select(func.count(ObjectType.id)).where(ObjectType.space_id == space.id)
        ).scalar_one()
        result.append(
            {
                "id": space.id,
                "key": space.key,
                "name": space.name,
                "iri": space.iri,
                "description": space.description,
                "domain": space.domain,
                "domain_label": packs.get_pack(space.domain).label,
                "type_count": int(type_count),
                "created_at": space.created_at.isoformat(sep=" ", timespec="seconds"),
            }
        )
    return result


def get_space(session, space_id: int) -> OntologySpace | None:
    return session.get(OntologySpace, space_id)


def space_summary(session, space_id: int) -> dict[str, Any]:
    space = session.get(OntologySpace, space_id)
    if space is None:
        return {"status": "missing", "space_id": space_id}
    pack = packs.get_pack(space.domain)

    def count(model, **filters) -> int:
        query = select(func.count(model.id))
        for key, value in filters.items():
            query = query.where(getattr(model, key) == value)
        return int(session.execute(query).scalar_one())

    pending = int(
        session.execute(
            select(func.count(ChangeProposal.id)).where(
                ChangeProposal.space_id == space_id, ChangeProposal.status == "pending"
            )
        ).scalar_one()
    )
    return {
        "id": space.id,
        "key": space.key,
        "name": space.name,
        "iri": space.iri,
        "description": space.description,
        "domain": space.domain,
        "pack_label": pack.label,
        "business_domains": pack.business_domains,
        "types": count(ObjectType, space_id=space_id),
        "properties": count(DataPropertyDef, space_id=space_id),
        "relations": count(RelationType, space_id=space_id),
        "links": count(TypeLink, space_id=space_id),
        "constraints": count(Constraint, space_id=space_id),
        "evidence": count(Evidence, space_id=space_id),
        "pending_proposals": pending,
    }


# --------------------------------------------------------------------------- #
# 唯一 IRI / 相似类型
# --------------------------------------------------------------------------- #
def unique_iri(session, space: OntologySpace, label: str) -> str:
    """同 IRI = 同类。中文标签派生的局部名会撞车，追加数字而不是覆盖既有类。"""
    base = slugify(label, fallback="Type")
    candidate = f"{space.iri}/{base}"
    suffix = 1
    while session.execute(select(ObjectType.id).where(ObjectType.iri == candidate)).scalars().first():
        suffix += 1
        candidate = f"{space.iri}/{base}{suffix}"
    return candidate


def find_similar(
    session, space_id: int, label: str, aliases: list[str] | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """提示疑似重复：别名包含 + 术语重叠度，避免「同义建类」。"""
    types = session.execute(
        select(ObjectType).where(ObjectType.space_id == space_id)
    ).scalars().all()
    alias_set = {a for a in (aliases or []) if a}
    scored: list[tuple[float, ObjectType, str]] = []
    for item in types:
        score = 0.0
        reason = ""
        if item.label == label:
            score = 1.0
            reason = "同名类型"
        elif label in (item.aliases or []) or item.label in alias_set:
            score = 0.9
            reason = "别名命中"
        else:
            overlap = overlap_ratio(label, item.label or "")
            if overlap >= 0.5:
                score = round(overlap * 0.7, 3)
                reason = f"术语重叠 {overlap:.0%}"
        if score > 0:
            scored.append((score, item, reason))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {
            "type_id": item.id,
            "label": item.label,
            "kind": item.kind,
            "definition": truncate(item.definition, 100),
            "score": score,
            "reason": reason,
        }
        for score, item, reason in scored[:limit]
    ]


# --------------------------------------------------------------------------- #
# 写入类型（唯一入口的下层实现）
# --------------------------------------------------------------------------- #
def _get_or_create_property(session, space: OntologySpace, spec: dict) -> DataPropertyDef:
    label = (spec.get("label") or "").strip()
    key = slugify(label, fallback="prop")
    prop = session.execute(
        select(DataPropertyDef).where(
            DataPropertyDef.space_id == space.id, DataPropertyDef.key == key
        )
    ).scalars().first()
    if prop is not None:
        return prop
    prop = DataPropertyDef(
        space_id=space.id,
        key=key,
        label=label,
        data_type=(spec.get("data_type") or "string").strip() or "string",
        unit=(spec.get("unit") or "").strip(),
        is_required=bool(spec.get("required")),
        enum_values=[str(x) for x in (spec.get("enum_values") or [])],
        description=(spec.get("description") or "").strip(),
    )
    session.add(prop)
    session.flush()
    return prop


def _resolve_parent(session, space_id: int, parent_label: str) -> ObjectType | None:
    if not parent_label:
        return None
    return session.execute(
        select(ObjectType).where(
            ObjectType.space_id == space_id, ObjectType.label == parent_label.strip()
        )
    ).scalars().first()


def create_type_record(
    session,
    space: OntologySpace,
    payload: dict,
    *,
    source: str = "manual",
    confidence: float | None = None,
) -> ObjectType:
    """落库一个对象类型（含属性与约束）；调用方负责事务与重复检查。"""
    label = (payload.get("label") or "").strip()
    if not label:
        raise ValueError("对象类型缺少 label")

    parent = _resolve_parent(session, space.id, payload.get("parent_label", ""))
    item = ObjectType(
        space_id=space.id,
        label=label,
        label_en=(payload.get("label_en") or "").strip(),
        definition=(payload.get("definition") or "").strip(),
        kind=(payload.get("kind") or "class").strip(),
        is_abstract=bool(payload.get("is_abstract")),
        parent_id=parent.id if parent else None,
        business_domain=(payload.get("business_domain") or "").strip(),
        confidence=float(confidence if confidence is not None else payload.get("confidence", 0.5)),
        source=source,
        aliases=[str(a) for a in (payload.get("aliases") or []) if str(a).strip()],
        tags=[str(t) for t in (payload.get("tags") or []) if str(t).strip()],
        annotations=payload.get("annotations") or {},
        iri=unique_iri(session, space, label),
        version=1,
        status="active",
    )
    session.add(item)
    session.flush()

    for ordinal, spec in enumerate(payload.get("properties") or []):
        if not isinstance(spec, dict) or not (spec.get("label") or "").strip():
            continue
        prop = _get_or_create_property(session, space, spec)
        session.add(
            TypeDataProperty(
                type_id=item.id,
                property_id=prop.id,
                is_required=bool(spec.get("required")),
                ordinal=ordinal,
                note=(spec.get("description") or "").strip(),
            )
        )

    for spec in payload.get("constraints") or []:
        if not isinstance(spec, dict):
            continue
        prop_label = (spec.get("property") or "").strip()
        prop = None
        if prop_label:
            prop = session.execute(
                select(DataPropertyDef).where(
                    DataPropertyDef.space_id == space.id, DataPropertyDef.label == prop_label
                )
            ).scalars().first()
        session.add(
            Constraint(
                space_id=space.id,
                type_id=item.id,
                property_id=prop.id if prop else None,
                kind=(spec.get("kind") or "range").strip(),
                severity=(spec.get("severity") or "violation").strip(),
                expression=(spec.get("expression") or "").strip(),
                message=(spec.get("message") or "").strip(),
                regulation_ref=(spec.get("regulation_ref") or "").strip(),
            )
        )

    session.add(
        Evidence(
            space_id=space.id,
            note=label,
            locator="领域包基线" if source == "pack" else "提案确认",
            quote=(payload.get("definition") or "")[:220],
        )
    )
    session.flush()
    return item


def get_or_create_relation(
    session, space: OntologySpace, label: str, category: str = "reference"
) -> RelationType:
    relation = session.execute(
        select(RelationType).where(
            RelationType.space_id == space.id, RelationType.label == label
        )
    ).scalars().first()
    if relation is not None:
        return relation

    # key 在空间内唯一，但不同标签可能 slug 相同（如「所属 VLAN」「归属 VLAN」），
    # 这里追加数字后缀而不是让唯一约束把安装打断。
    base_key = slugify(label, fallback="rel")
    key = base_key
    suffix = 1
    while session.execute(
        select(RelationType.id).where(
            RelationType.space_id == space.id, RelationType.key == key
        )
    ).scalars().first():
        suffix += 1
        key = f"{base_key}{suffix}"

    relation = RelationType(
        space_id=space.id,
        key=key,
        label=label,
        category=category or "reference",
        description="",
        is_builtin=False,
    )
    session.add(relation)
    session.flush()
    return relation


def apply_relations(session, space: OntologySpace, type_by_label: dict[str, ObjectType], specs: list[dict]) -> int:
    """建立类型间关联；目标不存在时跳过（不造假节点）。"""
    created = 0
    for spec in specs or []:
        if not isinstance(spec, dict):
            continue
        source_label = (spec.get("source") or "").strip()
        target_label = (spec.get("target") or "").strip()
        relation_label = (spec.get("relation") or "关联").strip()
        source = type_by_label.get(source_label)
        target = type_by_label.get(target_label)
        if source is None or target is None or source.id == target.id:
            continue
        exists = session.execute(
            select(TypeLink.id).where(
                TypeLink.source_type_id == source.id, TypeLink.target_type_id == target.id
            )
        ).scalars().first()
        if exists:
            continue
        relation = get_or_create_relation(session, space, relation_label, (spec.get("category") or "reference"))
        session.add(
            TypeLink(
                space_id=space.id,
                source_type_id=source.id,
                target_type_id=target.id,
                relation_id=relation.id,
                cardinality=(spec.get("cardinality") or "1..*"),
                label=relation_label,
                note=(spec.get("note") or ""),
            )
        )
        created += 1
    session.flush()
    return created


def _apply_type_relations(session, space: OntologySpace, item: ObjectType, payload: dict, type_by_label: dict) -> int:
    specs = [
        {"source": item.label, **rel} for rel in (payload.get("relations") or []) if isinstance(rel, dict)
    ]
    return apply_relations(session, space, type_by_label, specs)


# --------------------------------------------------------------------------- #
# 领域包安装（幂等）
# --------------------------------------------------------------------------- #
def _topological(entries: list[dict]) -> list[dict]:
    """按 parent_label 依赖排序，保证父类先落库。"""
    by_label = {item["label"]: item for item in entries if item.get("label")}
    ordered: list[dict] = []
    visited: set[str] = set()

    def visit(label: str, stack: set[str]) -> None:
        if label in visited or label in stack:
            return
        stack.add(label)
        item = by_label.get(label)
        if item is None:
            return
        parent = item.get("parent_label")
        if parent and parent in by_label:
            visit(parent, stack)
        visited.add(label)
        ordered.append(item)

    for label in by_label:
        visit(label, set())
    return ordered


def install_pack(
    session,
    pack_key: str,
    *,
    components,
    compile_wiki: bool = True,
    ingest_samples: bool = True,
    settings=None,
) -> dict[str, Any]:
    from ..wiki import compiler as wiki_compiler

    pack = packs.get_pack(pack_key)
    space = ensure_space(session, pack.key)

    existing_labels = {
        label
        for (label,) in session.execute(
            select(ObjectType.label).where(ObjectType.space_id == space.id)
        ).all()
    }

    created_types = 0
    type_by_label: dict[str, ObjectType] = {
        item.label: item
        for item in session.execute(
            select(ObjectType).where(ObjectType.space_id == space.id)
        ).scalars().all()
    }

    for entry in _topological(pack.object_types):
        label = entry.get("label", "")
        if not label or label in existing_labels:
            continue
        item = create_type_record(
            session,
            space,
            entry,
            source="pack",
            confidence=float(entry.get("confidence", 0.9)),
        )
        type_by_label[label] = item
        existing_labels.add(label)
        created_types += 1
    session.commit()

    relation_specs: list[dict] = list(pack.links)
    for entry in pack.object_types:
        if entry.get("label") not in existing_labels:
            continue
        for relation in entry.get("relations") or []:
            if isinstance(relation, dict) and relation.get("target"):
                relation_specs.append({"source": entry["label"], **relation})
    created_links = apply_relations(session, space, type_by_label, relation_specs)
    session.commit()

    ingested: list[dict] = []
    if ingest_samples:
        for doc in pack.samples:
            path = doc.path(pack.key)
            if not path.is_file():
                ingested.append({"filename": doc.filename, "status": "missing_file"})
                continue
            result = ingest_bytes(
                session,
                space.id,
                path.name,
                path.read_bytes(),
                components=components,
                title=doc.title,
                doc_type=doc.doc_type,
                issuer=doc.issuer,
                jurisdiction=doc.jurisdiction,
                authority=doc.authority,
                language=doc.language,
                chunk_size=getattr(settings, "chunk_size", 420) if settings else 420,
                chunk_overlap=getattr(settings, "chunk_overlap", 60) if settings else 60,
            )
            ingested.append(result.as_dict())

    wiki_stats = {"status": "skipped"}
    evidence_linked = 0
    if ingest_samples:
        evidence_linked = link_evidence_from_documents(session, space.id)

    if compile_wiki:
        wiki_stats = wiki_compiler.compile_space(
            session, space.id, only_dirty=False, author="compiler", message=f"安装领域包 {pack.key}"
        )

    return {
        "status": "installed",
        "pack": pack.key,
        "space_id": space.id,
        "space_name": space.name,
        "created_types": created_types,
        "created_links": created_links,
        "types_total": len(type_by_label),
        "documents": ingested,
        "wiki": wiki_stats,
    }


def link_evidence_from_documents(
    session, space_id: int, *, per_type: int = 2, max_types: int = 60
) -> int:
    """把「类型」与「实际出现它的文档片段」连起来，形成可点击的证据链。

    做法是最朴素的子串匹配——不做语义判断，因为证据链的要求恰好是**字面可核验**：
    点开必须能看到原句。语义相关但有争议的关联由提案机制处理。
    """
    from ...models import Document, DocumentChunk

    types = session.execute(
        select(ObjectType).where(ObjectType.space_id == space_id).limit(max_types)
    ).scalars().all()
    created = 0
    for item in types:
        existing = session.execute(
            select(Evidence.id).where(
                Evidence.space_id == space_id,
                Evidence.note == item.label,
                Evidence.document_id.isnot(None),
            )
        ).first()
        if existing:
            continue
        rows = session.execute(
            select(DocumentChunk, Document)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(Document.space_id == space_id, DocumentChunk.text.contains(item.label))
            .limit(per_type)
        ).all()
        for chunk, document in rows:
            session.add(
                Evidence(
                    space_id=space_id,
                    document_id=document.id,
                    chunk_id=chunk.id,
                    locator=f"{document.title}｜{chunk.locator}",
                    quote=truncate(chunk.text, 220),
                    note=item.label,
                )
            )
            created += 1
    session.flush()
    return created


# --------------------------------------------------------------------------- #
# 提案
# --------------------------------------------------------------------------- #
def create_proposal(
    session,
    space_id: int,
    action: str,
    payload: dict,
    *,
    rationale: str = "",
    author: str = "rag",
    evidence: list[dict] | None = None,
    agent_run_id: int | None = None,
    target_type_id: int | None = None,
) -> ChangeProposal:
    if action not in PROPOSAL_ACTIONS:
        raise ValueError(f"未知提案动作：{action}")
    proposal = ChangeProposal(
        space_id=space_id,
        action=action,
        target_type_id=target_type_id,
        payload=payload,
        rationale=rationale,
        status="pending",
        author=author,
        agent_run_id=agent_run_id,
    )
    session.add(proposal)
    session.flush()

    for item in evidence or []:
        session.add(
            Evidence(
                space_id=space_id,
                proposal_id=proposal.id,
                document_id=item.get("document_id"),
                chunk_id=item.get("chunk_id"),
                locator=(item.get("locator") or "").strip(),
                quote=(item.get("quote") or "").strip()[:500],
                note=(item.get("note") or payload.get("label") or "")[:120],
            )
        )
    session.flush()
    return proposal


def list_proposals(
    session, space_id: int, *, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    query = select(ChangeProposal).where(ChangeProposal.space_id == space_id)
    if status:
        query = query.where(ChangeProposal.status == status)
    rows = session.execute(
        query.order_by(ChangeProposal.created_at.desc()).limit(limit)
    ).scalars().all()
    result = []
    for item in rows:
        evidence_count = int(
            session.execute(
                select(func.count(Evidence.id)).where(Evidence.proposal_id == item.id)
            ).scalar_one()
        )
        result.append(
            {
                "id": item.id,
                "action": item.action,
                "action_label": kinds.action_label(item.action),
                "status": item.status,
                "status_label": kinds.status_label(item.status),
                "author": item.author,
                "label": (item.payload or {}).get("label", ""),
                "rationale": item.rationale,
                "evidence_count": evidence_count,
                "created_at": item.created_at.isoformat(sep=" ", timespec="seconds"),
                "decided_by": item.decided_by,
            }
        )
    return result


def proposal_detail(session, proposal_id: int) -> dict[str, Any] | None:
    item = session.get(ChangeProposal, proposal_id)
    if item is None:
        return None
    evidence = session.execute(
        select(Evidence).where(Evidence.proposal_id == proposal_id)
    ).scalars().all()
    return {
        "id": item.id,
        "space_id": item.space_id,
        "action": item.action,
        "action_label": kinds.action_label(item.action),
        "status": item.status,
        "status_label": kinds.status_label(item.status),
        "author": item.author,
        "payload": item.payload or {},
        "rationale": item.rationale,
        "target_type_id": item.target_type_id,
        "created_at": item.created_at.isoformat(sep=" ", timespec="seconds"),
        "decided_at": item.decided_at.isoformat(sep=" ", timespec="seconds") if item.decided_at else "",
        "decided_by": item.decided_by,
        "decision_comment": item.decision_comment,
        "evidence": [
            {
                "id": row.id,
                "document_id": row.document_id,
                "locator": row.locator,
                "quote": row.quote,
                "note": row.note,
            }
            for row in evidence
        ],
    }


def _mark_related_pages_dirty(session, space_id: int, type_ids: list[int]) -> None:
    from ..wiki.compiler import mark_dirty

    slugs = ["index", "quality", "relations"] + [f"type-{tid}" for tid in type_ids]
    type_rows = session.execute(
        select(ObjectType.id, ObjectType.business_domain).where(ObjectType.space_id == space_id)
    ).all()
    for _tid, domain in type_rows:
        if domain:
            slugs.append(f"domain-{slugify(domain)}")
    mark_dirty(session, space_id, slugs)


def accept_proposal(
    session,
    proposal_id: int,
    *,
    actor: str = "local",
    components=None,
    settings=None,
) -> dict[str, Any]:
    """**唯一**改变本体结构的入口：落库 → 置脏 → 增量重编译 → IM 通知。"""
    from ..wiki import compiler as wiki_compiler

    proposal = session.get(ChangeProposal, proposal_id)
    if proposal is None:
        return {"status": "missing", "proposal_id": proposal_id}
    if proposal.status != "pending":
        return {"status": "already_decided", "proposal_id": proposal_id, "state": proposal.status}

    space = session.get(OntologySpace, proposal.space_id)
    if space is None:
        return {"status": "space_missing", "proposal_id": proposal_id}

    payload = proposal.payload or {}
    affected: list[int] = []
    result: dict[str, Any] = {"status": "accepted", "proposal_id": proposal_id, "action": proposal.action}

    if proposal.action == "create_type":
        label = (payload.get("label") or "").strip()
        duplicate = session.execute(
            select(ObjectType).where(
                ObjectType.space_id == space.id, ObjectType.label == label
            )
        ).scalars().first()
        if duplicate is not None:
            return {
                "status": "conflict",
                "proposal_id": proposal_id,
                "message": f"类型「{label}」已存在（#{duplicate.id}），请驳回或改为修改提案",
                "existing_type_id": duplicate.id,
            }
        item = create_type_record(
            session, space, payload, source=proposal.author or "rag",
            confidence=float(payload.get("confidence", 0.6)),
        )
        type_by_label = {
            row.label: row
            for row in session.execute(
                select(ObjectType).where(ObjectType.space_id == space.id)
            ).scalars().all()
        }
        _apply_type_relations(session, space, item, payload, type_by_label)
        affected.append(item.id)
        result["type_id"] = item.id
        result["label"] = item.label
        result["iri"] = item.iri

    elif proposal.action == "update_type":
        type_id = proposal.target_type_id or payload.get("type_id")
        item = session.get(ObjectType, int(type_id)) if type_id else None
        if item is None:
            return {"status": "target_missing", "proposal_id": proposal_id}
        for field in ("definition", "label_en", "business_domain", "kind"):
            if payload.get(field):
                setattr(item, field, payload[field])
        if payload.get("aliases"):
            merged = list(dict.fromkeys([*(item.aliases or []), *payload["aliases"]]))
            item.aliases = merged
        if payload.get("tags"):
            item.tags = list(dict.fromkeys([*(item.tags or []), *payload["tags"]]))
        if payload.get("confidence") is not None:
            item.confidence = float(payload["confidence"])
        item.version += 1
        item.updated_at = utcnow()
        affected.append(item.id)
        result.update({"type_id": item.id, "version": item.version})

    elif proposal.action == "create_link":
        type_by_label = {
            row.label: row
            for row in session.execute(
                select(ObjectType).where(ObjectType.space_id == space.id)
            ).scalars().all()
        }
        created = apply_relations(session, space, type_by_label, [payload])
        if not created:
            return {"status": "conflict", "proposal_id": proposal_id, "message": "关联两端不存在或已存在"}
        for label in (payload.get("source"), payload.get("target")):
            if label in type_by_label:
                affected.append(type_by_label[label].id)
        result["created_links"] = created

    elif proposal.action == "create_constraint":
        type_id = proposal.target_type_id or payload.get("type_id")
        item = session.get(ObjectType, int(type_id)) if type_id else None
        if item is None:
            return {"status": "target_missing", "proposal_id": proposal_id}
        prop_label = (payload.get("property") or "").strip()
        prop = None
        if prop_label:
            prop = session.execute(
                select(DataPropertyDef).where(
                    DataPropertyDef.space_id == space.id, DataPropertyDef.label == prop_label
                )
            ).scalars().first()
        session.add(
            Constraint(
                space_id=space.id,
                type_id=item.id,
                property_id=prop.id if prop else None,
                kind=payload.get("kind") or "range",
                severity=payload.get("severity") or "violation",
                expression=payload.get("expression") or "",
                message=payload.get("message") or "",
                regulation_ref=payload.get("regulation_ref") or "",
            )
        )
        affected.append(item.id)
        result["constraint_type_id"] = item.id

    proposal.status = "accepted"
    proposal.decided_at = _now()
    proposal.decided_by = actor
    session.commit()

    _mark_related_pages_dirty(session, space.id, affected)
    session.commit()

    wiki_stats: dict[str, Any] = {"status": "skipped"}
    if settings is None or getattr(settings, "wiki_compile_on_accept", True):
        wiki_stats = wiki_compiler.compile_space(
            session, space.id, only_dirty=True, author="compiler", message=f"提案 #{proposal_id} 确认"
        )
    result["wiki"] = wiki_stats

    if components is not None:
        label = payload.get("label") or f"提案 #{proposal_id}"
        message = f"本体已更新：{kinds.action_label(proposal.action)}「{label}」"
        result["notified"] = components.im.send(message, title="FinOnto · 提案确认")

    return result


def reject_proposal(
    session, proposal_id: int, *, actor: str = "local", comment: str = ""
) -> dict[str, Any]:
    proposal = session.get(ChangeProposal, proposal_id)
    if proposal is None:
        return {"status": "missing", "proposal_id": proposal_id}
    if proposal.status != "pending":
        return {"status": "already_decided", "state": proposal.status}
    proposal.status = "rejected"
    proposal.decided_at = _now()
    proposal.decided_by = actor
    proposal.decision_comment = comment
    session.commit()
    return {"status": "rejected", "proposal_id": proposal_id}


def _now() -> datetime:
    return utcnow()


# --------------------------------------------------------------------------- #
# 草案生成（RAG / Agent 共用）
# --------------------------------------------------------------------------- #
def build_draft_prompt(
    space: OntologySpace,
    *,
    corpus: str,
    question: str = "",
    max_types: int = 4,
) -> str:
    pack = packs.get_pack(space.domain)
    lines = [
        "# 任务：ontology_draft",
        f"【问题】\n{question or '从语料中抽取可建模的对象类型'}",
        f"【领域】\n{pack.prompt_context()}",
        f"【子域线索】\n{'、'.join(pack.business_domains)}",
        f"【标签线索】\n{'、'.join(pack.tag_cues)}",
        f"【术语后缀】\n{'、'.join(pack.term_suffixes)}",
        f"【句式线索】\n{'、'.join(pack.clause_markers)}",
        f"【语料】\n{corpus[:6000]}",
        f"要求：产出不超过 {max_types} 个候选对象类型，输出 JSON："
        '{"summary":"...","object_types":[{"label":"...","kind":"class","definition":"...",'
        '"business_domain":"...","properties":[{"label":"...","data_type":"string","required":true}],'
        '"relations":[{"relation":"...","target":"..."}],'
        '"constraints":[{"kind":"range","expression":"...","message":"..."}],'
        '"evidence":[{"quote":"...","locator":"..."}]}]}',
    ]
    return "\n\n".join(lines)


def _parse_json_block(raw: str) -> dict:
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def draft_proposals(
    session,
    space_id: int,
    *,
    corpus: str,
    components,
    question: str = "",
    author: str = "rag",
    max_types: int = 4,
    evidence_from_chunks: list[dict] | None = None,
) -> dict[str, Any]:
    """调用模型产出草案 JSON，逐条落成待确认提案（不写本体）。"""
    space = session.get(OntologySpace, space_id)
    if space is None:
        return {"status": "space_missing", "space_id": space_id}

    existing_labels = [
        row.label
        for row in session.execute(
            select(ObjectType).where(ObjectType.space_id == space_id)
        ).scalars().all()
    ]
    prompt = build_draft_prompt(space, corpus=corpus, question=question, max_types=max_types)
    prompt += f"\n\n【已有类型】\n{'、'.join(existing_labels) or '（空）'}\n避免重复产出已有类型。"
    raw = components.llm.chat([Message(role="user", content=prompt)], max_tokens=1400)
    try:
        data = _parse_json_block(raw)
    except json.JSONDecodeError:
        return {"status": "parse_failed", "raw": truncate(raw, 400)}

    created: list[dict[str, Any]] = []
    skipped: list[str] = []
    entries = [entry for entry in (data.get("object_types") or []) if isinstance(entry, dict)]
    # 候选名粗筛：像整句的（「X 三类合称 Y」）不建类型，但保留在 skipped 里可追溯
    from ...providers.llm import looks_like_concept

    good = []
    for entry in entries:
        label = (entry.get("label") or "").strip()
        if label and looks_like_concept(label):
            good.append(entry)
        elif label:
            skipped.append(label)
    # 全被筛掉时保底保留第一条，避免「模型给了东西但一条提案都没有」
    if not good and entries:
        good = entries[:1]

    for entry in good[:max_types]:
        if not (entry.get("label") or "").strip():
            continue
        label = entry["label"].strip()
        similar = find_similar(session, space_id, label, entry.get("aliases") or [])
        evidence: list[dict] = []
        for quote in entry.get("evidence") or []:
            if isinstance(quote, dict):
                evidence.append(
                    {
                        "quote": quote.get("quote", ""),
                        "locator": quote.get("locator", ""),
                        "note": label,
                    }
                )
        for chunk in (evidence_from_chunks or [])[:3]:
            evidence.append(
                {
                    "document_id": chunk.get("document_id"),
                    "chunk_id": chunk.get("chunk_id"),
                    "locator": chunk.get("locator", ""),
                    "quote": (chunk.get("text") or "")[:300],
                    "note": label,
                }
            )
        proposal = create_proposal(
            session,
            space_id,
            "create_type",
            entry,
            rationale=f"{data.get('summary', '')}".strip() or "由语料抽取的候选类型",
            author=author,
            evidence=evidence,
        )
        created.append(
            {
                "proposal_id": proposal.id,
                "label": label,
                "similar": similar,
                "evidence_count": len(evidence),
                "payload": entry,
            }
        )
    session.commit()
    return {
        "status": "drafted",
        "summary": data.get("summary", ""),
        "rationale": data.get("rationale", ""),
        "proposals": created,
        "skipped_labels": skipped,
    }


# --------------------------------------------------------------------------- #
# 校验与人工建类
# --------------------------------------------------------------------------- #
def validate_space(session, space_id: int) -> dict[str, list[dict[str, Any]]]:
    report: dict[str, list[dict[str, Any]]] = {"violation": [], "warning": [], "info": []}

    types = session.execute(
        select(ObjectType).where(ObjectType.space_id == space_id)
    ).scalars().all()
    by_id = {item.id: item for item in types}

    def add(level: str, item: ObjectType | None, code: str, message: str) -> None:
        report[level].append(
            {
                "code": code,
                "type_id": item.id if item else 0,
                "type_label": item.label if item else "空间",
                "message": message,
            }
        )

    # 父类环检测
    for item in types:
        seen: set[int] = set()
        current = item
        while current is not None and current.parent_id:
            if current.parent_id in seen:
                add("violation", item, "parent_cycle", f"父子分类出现环：{item.label}")
                break
            seen.add(current.parent_id)
            current = by_id.get(current.parent_id)

    for item in types:
        if not (item.definition or "").strip():
            add("warning", item, "no_definition", "缺少定义，Wiki 页会显示占位文本")
        if not (item.business_domain or "").strip():
            add("info", item, "no_domain", "未归属业务域，导航与图谱洞察会把其归到「未分域」")
        props = session.execute(
            select(func.count(TypeDataProperty.id)).where(TypeDataProperty.type_id == item.id)
        ).scalar_one()
        if int(props) == 0:
            add("warning", item, "no_property", "没有任何数据属性，无法承载结构化数据")
        evidence_count = int(
            session.execute(
                select(func.count(Evidence.id)).where(
                    Evidence.space_id == space_id, Evidence.note == item.label
                )
            ).scalar_one()
        )
        if evidence_count == 0:
            add("info", item, "no_evidence", "没有关联证据，建议补充来源引文")

    constraints = session.execute(
        select(Constraint).where(Constraint.space_id == space_id, Constraint.is_active.is_(True))
    ).scalars().all()
    for constraint in constraints:
        expression = (constraint.expression or "").strip()
        if not expression:
            add("warning", by_id.get(constraint.type_id), "empty_expression", "约束表达式为空")
            continue
        if not re.search(r"[<>=!]|则|必须|in\s*\(|并且|或", expression):
            add(
                "warning",
                by_id.get(constraint.type_id),
                "unparsable_expression",
                f"约束表达式无法解析：{truncate(expression, 60)}",
            )

    links = session.execute(select(TypeLink).where(TypeLink.space_id == space_id)).scalars().all()
    for link in links:
        if link.source_type_id not in by_id or link.target_type_id not in by_id:
            add("violation", by_id.get(link.source_type_id), "dangling_link", "关联指向了不存在的类型")

    return report


def create_type_manual(session, space_id: int, payload: dict, *, actor: str = "local") -> dict[str, Any]:
    """本体工作室「手工新建」：与提案确认走同一套落库逻辑（但不需要提案）。"""
    space = session.get(OntologySpace, space_id)
    if space is None:
        return {"status": "space_missing"}
    label = (payload.get("label") or "").strip()
    if not label:
        return {"status": "invalid", "message": "label 必填"}
    duplicate = session.execute(
        select(ObjectType).where(ObjectType.space_id == space_id, ObjectType.label == label)
    ).scalars().first()
    if duplicate is not None:
        return {"status": "conflict", "message": f"类型「{label}」已存在", "existing_type_id": duplicate.id}

    item = create_type_record(session, space, payload, source=payload.get("source", "manual"))
    type_by_label = {
        row.label: row
        for row in session.execute(
            select(ObjectType).where(ObjectType.space_id == space_id)
        ).scalars().all()
    }
    _apply_type_relations(session, space, item, payload, type_by_label)
    session.commit()

    from ..wiki import compiler as wiki_compiler

    mark = _mark_related_pages_dirty(session, space_id, [item.id])
    session.commit()
    wiki_stats = wiki_compiler.compile_space(
        session, space_id, only_dirty=True, author="compiler", message=f"手工新建类型 {label}"
    )
    return {
        "status": "created",
        "type_id": item.id,
        "label": item.label,
        "iri": item.iri,
        "dirty_pages": mark,
        "wiki": wiki_stats,
        "validation": validate_space(session, space_id),
    }


def update_type_definition(
    session, type_id: int, payload: dict, *, actor: str = "local"
) -> dict[str, Any]:
    item = session.get(ObjectType, type_id)
    if item is None:
        return {"status": "missing"}
    changed = False
    for field in ("definition", "label_en", "business_domain", "kind"):
        value = payload.get(field)
        if value is not None and value != getattr(item, field):
            setattr(item, field, value)
            changed = True
    if changed:
        item.version += 1
        item.updated_at = utcnow()
        session.commit()
        from ..wiki import compiler as wiki_compiler

        page = session.execute(
            select(WikiPage).where(
                WikiPage.space_id == item.space_id, WikiPage.slug == f"type-{item.id}"
            )
        ).scalars().first()
        if page is not None:
            page.is_dirty = True
            session.commit()
        wiki_compiler.compile_space(
            session, item.space_id, only_dirty=True, author="compiler", message=f"修改类型 {item.label}"
        )
    return {"status": "updated" if changed else "unchanged", "type_id": item.id, "version": item.version}


def delete_type(session, type_id: int) -> dict[str, Any]:
    item = session.get(ObjectType, type_id)
    if item is None:
        return {"status": "missing"}
    space_id = item.space_id
    label = item.label
    session.query(TypeLink).filter(
        (TypeLink.source_type_id == type_id) | (TypeLink.target_type_id == type_id)
    ).delete(synchronize_session=False)
    session.query(TypeDataProperty).filter(TypeDataProperty.type_id == type_id).delete(synchronize_session=False)
    session.query(Constraint).filter(Constraint.type_id == type_id).delete(synchronize_session=False)
    session.query(Evidence).filter(Evidence.note == label).delete(synchronize_session=False)
    session.delete(item)
    session.commit()

    from ..wiki import compiler as wiki_compiler

    _mark_related_pages_dirty(session, space_id, [])
    session.commit()
    wiki_compiler.compile_space(session, space_id, only_dirty=True, author="compiler", message=f"删除类型 {label}")
    return {"status": "deleted", "type_id": type_id}


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
def list_types(
    session,
    space_id: int,
    *,
    keyword: str = "",
    business_domain: str = "",
    kind: str = "",
) -> list[dict[str, Any]]:
    query = select(ObjectType).where(ObjectType.space_id == space_id)
    if kind:
        query = query.where(ObjectType.kind == kind)
    if business_domain:
        query = query.where(ObjectType.business_domain == business_domain)
    rows = session.execute(query.order_by(ObjectType.label)).scalars().all()
    if keyword:
        needle = keyword.strip().lower()
        rows = [
            item
            for item in rows
            if needle in (item.label or "").lower()
            or needle in (item.definition or "").lower()
            or any(needle in (alias or "").lower() for alias in (item.aliases or []))
        ]
    result = []
    for item in rows:
        prop_count = int(
            session.execute(
                select(func.count(TypeDataProperty.id)).where(TypeDataProperty.type_id == item.id)
            ).scalar_one()
        )
        link_count = int(
            session.execute(
                select(func.count(TypeLink.id)).where(TypeLink.source_type_id == item.id)
            ).scalar_one()
        )
        result.append(
            {
                "id": item.id,
                "label": item.label,
                "kind": item.kind,
                "kind_label": kinds.kind_label(item.kind),
                "business_domain": item.business_domain,
                "confidence": round(float(item.confidence or 0), 2),
                "source": item.source,
                "version": item.version,
                "property_count": prop_count,
                "link_count": link_count,
                "is_abstract": item.is_abstract,
                "definition": truncate(item.definition, 120),
            }
        )
    return result


def type_detail(session, type_id: int) -> dict[str, Any] | None:
    from ..wiki.compiler import _constraint_rows, _link_rows, _property_rows

    item = session.get(ObjectType, type_id)
    if item is None:
        return None
    evidence = session.execute(
        select(Evidence).where(Evidence.note == item.label, Evidence.space_id == item.space_id)
    ).scalars().all()
    parent = session.get(ObjectType, item.parent_id) if item.parent_id else None
    children = session.execute(
        select(ObjectType).where(ObjectType.parent_id == item.id)
    ).scalars().all()
    return {
        "id": item.id,
        "space_id": item.space_id,
        "label": item.label,
        "label_en": item.label_en,
        "definition": item.definition,
        "kind": item.kind,
        "kind_label": kinds.kind_label(item.kind),
        "is_abstract": item.is_abstract,
        "business_domain": item.business_domain,
        "confidence": item.confidence,
        "source": item.source,
        "status": item.status,
        "version": item.version,
        "iri": item.iri,
        "aliases": item.aliases or [],
        "tags": item.tags or [],
        "parent": {"id": parent.id, "label": parent.label} if parent else None,
        "children": [{"id": child.id, "label": child.label} for child in children],
        "properties": _property_rows(session, item.id),
        "links": _link_rows(session, item.id),
        "constraints": _constraint_rows(session, item.id),
        "evidence": [
            {"locator": row.locator, "quote": truncate(row.quote, 240), "document_id": row.document_id}
            for row in evidence
        ],
        "wiki_slug": f"type-{item.id}",
    }
