"""本体空间、对象类型、关联、领域包与导出接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import SERVER_DIR
from ..core.ontology.owl import export_bundle
from ..core.ontology.packs import catalog, install_pack, require_pack, seed_space, vocab_for
from ..core.ontology.service import OntologyService
from ..db import get_session
from ..models import ChangeProposal, Space
from ..services.orchestrator import OntologyDraftService, after_accept

router = APIRouter(prefix="/api/ontology", tags=["ontology"])


def _service(session: Session) -> OntologyService:
    return OntologyService(session)


# ------------------------------------------------------------------- 领域包目录
@router.get("/packs")
def packs(session: Session = Depends(get_session)) -> dict[str, Any]:
    """内置领域包画廊：目录信息、是否已安装、对应空间与示例语料可用性。"""
    return {"packs": catalog(session, SERVER_DIR)}


@router.post("/packs/{key}/install")
def install(
    key: str,
    session: Session = Depends(get_session),
    load_samples: bool = True,
    compile_wiki: bool = True,
) -> dict[str, Any]:
    """一键安装领域包：建空间 → 灌基线本体 → 示例语料入库 → 编译 Wiki（全程幂等）。"""
    try:
        pack = require_pack(key)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    report = install_pack(
        session, pack, SERVER_DIR, load_samples=load_samples, compile_wiki=compile_wiki
    )
    session.commit()
    return report


@router.get("/spaces")
def list_spaces(session: Session = Depends(get_session)) -> dict[str, Any]:
    service = _service(session)
    rows = list(session.query(Space).all())
    return {
        "spaces": [
            {
                "id": space.id,
                "name": space.name,
                "domain": space.domain,
                "description": space.description,
                "iri": space.iri,
                "status": space.status,
                "created_at": space.created_at.isoformat() if space.created_at else "",
                "stats": service.space_stats(space.id),
            }
            for space in rows
        ]
    }


@router.post("/spaces")
def create_space(body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "空间名称不能为空")
    domain = str(body.get("domain", "general")).strip() or "general"
    space = _service(session).create_space(
        name=name,
        domain=domain,
        description=str(body.get("description", "")),
        iri=str(body.get("iri", "")),
    )
    session.commit()
    return {"id": space.id, "name": space.name, "domain": space.domain}


@router.post("/spaces/{space_id}/seed")
def seed_into_space(space_id: int, session: Session = Depends(get_session), pack: str = "") -> dict[str, Any]:
    """把某个领域包的基线本体灌入指定空间；不指定 pack 时使用空间自身所属领域。"""
    space = session.get(Space, space_id)
    if space is None:
        raise HTTPException(404, "空间不存在")
    try:
        target = require_pack(pack or space.domain)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    info = seed_space(session, target, space)
    session.commit()
    return {"space_id": space_id, "seed": info, "pack": target.key, "pack_label": target.label}


@router.get("/vocab")
def vocabulary(session: Session = Depends(get_session), space_id: int | None = None) -> dict[str, Any]:
    """受控词表：通用内核词汇 + 该空间所属领域包的扩展维度。"""
    return vocab_for(session, space_id)


@router.get("/{space_id}/summary")
def space_summary(space_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    service = _service(session)
    issues = service.validate_space(space_id)
    return {
        "stats": service.space_stats(space_id),
        "issues": [vars(i) for i in issues],
        "relations": [
            {
                "id": rel.id,
                "label": rel.label,
                "label_en": rel.label_en,
                "category": rel.category,
                "description": rel.description,
            }
            for rel in service.list_relation_types(space_id)
        ],
        "properties": [
            {
                "id": p.id,
                "label": p.label,
                "datatype": p.datatype,
                "unit": p.unit,
                "pattern": p.pattern,
                "description": p.description,
                "enum_id": p.enum_id,
            }
            for p in service.list_properties(space_id)
        ],
        "constraints": [
            {
                "id": c.id,
                "name": c.name,
                "kind": c.kind,
                "expression": c.expression,
                "severity": c.severity,
                "reg_ref": c.reg_ref,
                "subject_type_id": c.subject_type_id,
                "subject_property_iri": c.subject_property_iri,
                "description": c.description,
                "active": c.active,
            }
            for c in service.list_constraints(space_id)
        ],
    }


@router.get("/{space_id}/types")
def list_types(
    space_id: int,
    session: Session = Depends(get_session),
    q: str | None = None,
    status: str | None = None,
    domain: str | None = None,
    kind: str | None = None,
    limit: int = 300,
) -> dict[str, Any]:
    rows = _service(session).list_types(space_id, q=q, status=status, domain=domain, kind=kind, limit=limit)
    return {
        "types": [
            {
                "id": row.id,
                "iri": row.iri,
                "label": row.label,
                "label_en": row.label_en,
                "definition": row.definition,
                "kind": row.kind,
                "status": row.status,
                "abstract": row.abstract,
                "confidence": row.confidence,
                "origin": row.origin,
                "business_domain": row.business_domain,
                "parent_id": row.parent_id,
                "tags": row.tags,
                "aliases": row.aliases,
                "version_no": row.version_no,
            }
            for row in rows
        ],
        "total": len(rows),
    }


@router.post("/{space_id}/types")
def create_type(space_id: int, body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    service = _service(session)
    issues = service.validate_type_payload(body, space_id)
    blocking = [i for i in issues if i.level == "violation"]
    if blocking:
        raise HTTPException(422, detail=[vars(i) for i in blocking])
    row = service.create_type(space_id, body, origin=str(body.get("origin", "manual")))
    session.commit()
    return {"type": service.detail(row.id), "issues": [vars(i) for i in issues]}


@router.get("/types/{type_id}")
def type_detail(type_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    service = _service(session)
    if service.get_type(type_id) is None:
        raise HTTPException(404, "对象类型不存在")
    return service.detail(type_id)


@router.patch("/types/{type_id}")
def update_type(type_id: int, body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    service = _service(session)
    row = service.update_type(type_id, body)
    session.commit()
    return {"type": service.detail(row.id)}


@router.delete("/types/{type_id}")
def delete_type(type_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    service = _service(session)
    row = service.get_type(type_id)
    if row is None:
        raise HTTPException(404, "对象类型不存在")
    space_id = row.space_id
    service.delete_type(type_id)
    session.commit()
    return {"deleted": type_id, "space_id": space_id}


@router.post("/{space_id}/links")
def create_link(space_id: int, body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    service = _service(session)
    try:
        link = service.link(
            space_id,
            int(body["source_id"]),
            int(body["target_id"]),
            body.get("relation", "关联"),
            description=str(body.get("description", "")),
            cardinality=str(body.get("cardinality", "n:m")),
            confidence=float(body.get("confidence", 0.6)),
            status=str(body.get("status", "active")),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    session.commit()
    return {"link_id": link.id, "relation_id": link.relation_id}


@router.delete("/links/{link_id}")
def delete_link(link_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    from ..models import TypeLink

    link = session.get(TypeLink, link_id)
    if link is None:
        raise HTTPException(404, "关联不存在")
    session.delete(link)
    session.commit()
    return {"deleted": link_id}


@router.get("/{space_id}/similar")
def similar_types(space_id: int, label: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    return {"label": label, "duplicates": _service(session).similar_types(space_id, label)}


@router.get("/{space_id}/validate")
def validate(space_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    issues = _service(session).validate_space(space_id)
    return {"issues": [vars(i) for i in issues], "blocking": sum(1 for i in issues if i.level == "violation")}


# ------------------------------------------------------------------ 提案工作流
@router.get("/{space_id}/proposals")
def list_proposals(
    space_id: int, session: Session = Depends(get_session), status: str | None = None, limit: int = 100
) -> dict[str, Any]:
    service = _service(session)
    rows = service.list_proposals(space_id, status=status, limit=limit)
    return {"proposals": [service.proposal_view(p) for p in rows]}


@router.post("/{space_id}/proposals")
def create_proposal(space_id: int, body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    """人工（或外部系统）直接提交提案，与 Agent 走同一条确认链路。"""
    service = _service(session)
    kind = str(body.get("kind", "object_type"))
    payload = body.get("payload") or {}
    if kind == "object_type":
        issues = service.validate_type_payload(payload, space_id)
        blocking = [i for i in issues if i.level == "violation"]
        if blocking:
            raise HTTPException(422, detail=[vars(i) for i in blocking])
    proposal = service.create_proposal(
        space_id,
        kind=kind,
        payload=payload,
        rationale=str(body.get("rationale", "")),
        confidence=float(body.get("confidence", 0.6)),
        evidences=body.get("evidences") or [],
        title=str(body.get("title", "")),
    )
    session.commit()
    return {"proposal": service.proposal_view(proposal)}


@router.post("/proposals/{proposal_id}/accept")
def accept_proposal(
    proposal_id: int, body: dict[str, Any] | None = None, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """人工确认：写入本体 → 增量重编译 Wiki → 按配置的 IM 渠道通知。"""
    service = _service(session)
    body = body or {}
    try:
        result = service.accept_proposal(
            proposal_id,
            reviewer=str(body.get("reviewer", "human")),
            patch=body.get("patch") or None,
        )
    except KeyError:
        raise HTTPException(404, "提案不存在") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    proposal = session.get(ChangeProposal, proposal_id)
    side_effects = after_accept(session, proposal.space_id, proposal, result, notify=bool(body.get("notify", True)))
    session.commit()
    return {
        "result": {
            "proposal_id": result.proposal_id,
            "created_types": result.created_types,
            "created_links": result.created_links,
            "dirty_pages": result.dirty_pages,
            "issues": [vars(i) for i in result.issues],
        },
        "proposal": service.proposal_view(proposal),
        "side_effects": side_effects,
    }


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int, body: dict[str, Any] | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    service = _service(session)
    try:
        proposal = service.reject_proposal(proposal_id, note=str((body or {}).get("note", "")), reviewer=str((body or {}).get("reviewer", "human")))
    except KeyError:
        raise HTTPException(404, "提案不存在") from None
    session.commit()
    return {"proposal": service.proposal_view(proposal)}


@router.patch("/proposals/{proposal_id}")
def patch_proposal(proposal_id: int, body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    service = _service(session)
    try:
        proposal = service.update_proposal(proposal_id, body)
    except KeyError:
        raise HTTPException(404, "提案不存在") from None
    session.commit()
    return {"proposal": service.proposal_view(proposal)}


# ------------------------------------------------------- 问答驱动的草稿与提案
@router.post("/{space_id}/draft")
async def draft_from_question(space_id: int, body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    """问答 → 检索证据 → 本体草稿（仅预览，不写库）。"""
    question = str(body.get("question", "")).strip()
    if not question:
        raise HTTPException(400, "question 不能为空")
    return await OntologyDraftService(session, space_id).draft(question, final_k=body.get("final_k"))


@router.post("/{space_id}/draft/propose")
async def propose_from_question(space_id: int, body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    """问答 → 草稿 → 待确认提案。"""
    question = str(body.get("question", "")).strip()
    if not question:
        raise HTTPException(400, "question 不能为空")
    return await OntologyDraftService(session, space_id).propose(question, final_k=body.get("final_k"))


@router.get("/{space_id}/export")
def export(space_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    bundle = export_bundle(session, space_id)
    return {"files": bundle, "count": len(bundle)}
