"""本体接口：类型、提案、校验、导出、草案生成。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from ..core import audit
from ..core.ontology import owl, service
from ..db import get_db
from ..middleware import require_role
from ..providers.registry import get_components

router = APIRouter(prefix="/api/ontology", tags=["ontology"])


# --------------------------------------------------------------------------- #
# 类型
# --------------------------------------------------------------------------- #
@router.get("/{space_id}/types")
def list_types(
    space_id: int,
    keyword: str = Query(""),
    domain: str = Query(""),
    kind: str = Query(""),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items = service.list_types(db, space_id, keyword=keyword, business_domain=domain, kind=kind)
    return {"types": items, "count": len(items)}


@router.get("/types/{type_id}")
def type_detail(type_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    detail = service.type_detail(db, type_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"类型不存在：{type_id}")
    return detail


@router.post("/{space_id}/types")
def create_type(space_id: int, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    result = service.create_type_manual(db, space_id, payload)
    if result.get("status") == "space_missing":
        raise HTTPException(status_code=404, detail="空间不存在")
    if result.get("status") == "conflict":
        raise HTTPException(status_code=409, detail=result.get("message", "类型已存在"))
    if result.get("status") == "invalid":
        raise HTTPException(status_code=400, detail=result.get("message", "参数不合法"))
    return result


@router.patch("/types/{type_id}")
def update_type(type_id: int, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    result = service.update_type_definition(db, type_id, payload)
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail=f"类型不存在：{type_id}")
    return result


@router.delete("/types/{type_id}")
def delete_type(type_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = service.delete_type(db, type_id)
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail=f"类型不存在：{type_id}")
    return result


@router.get("/{space_id}/validate")
def validate(space_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    report = service.validate_space(db, space_id)
    return {
        "space_id": space_id,
        **report,
        "counts": {level: len(items) for level, items in report.items()},
    }


@router.get("/{space_id}/similar")
def similar(
    space_id: int, label: str = Query(...), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return {"label": label, "similar": service.find_similar(db, space_id, label)}


@router.get("/{space_id}/summary")
def summary(space_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return service.space_summary(db, space_id)


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
@router.get("/{space_id}/export")
def export(
    space_id: int,
    format: str = Query("turtle", pattern="^(turtle|skos|json)$"),
    db: Session = Depends(get_db),
):
    if service.get_space(db, space_id) is None:
        raise HTTPException(status_code=404, detail="空间不存在")
    if format == "turtle":
        return PlainTextResponse(owl.export_turtle(db, space_id), media_type="text/turtle; charset=utf-8")
    if format == "skos":
        return PlainTextResponse(owl.export_skos(db, space_id), media_type="text/turtle; charset=utf-8")
    return {"schemas": owl.export_json_schemas(db, space_id)}


# --------------------------------------------------------------------------- #
# 提案
# --------------------------------------------------------------------------- #
@router.get("/{space_id}/proposals")
def proposals(
    space_id: int,
    status: str = Query(""),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items = service.list_proposals(db, space_id, status=status or None)
    return {"proposals": items, "count": len(items)}


@router.get("/proposals/{proposal_id}")
def proposal_detail(proposal_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    detail = service.proposal_detail(db, proposal_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"提案不存在：{proposal_id}")
    return detail


@router.post("/proposals/{proposal_id}/accept")
def accept_proposal(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_role("approver")),
) -> dict[str, Any]:
    result = service.accept_proposal(db, proposal_id, components=get_components())
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail=f"提案不存在：{proposal_id}")
    audit.record(
        db,
        action="accept_proposal",
        target=f"proposal:{proposal_id}",
        status="ok" if result.get("status") == "accepted" else "denied",
        detail={"result": result.get("status"), "label": result.get("label", "")},
        request=request,
    )
    db.commit()
    return result


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(
    proposal_id: int,
    request: Request,
    comment: str = Query(""),
    db: Session = Depends(get_db),
    user=Depends(require_role("approver")),
) -> dict[str, Any]:
    result = service.reject_proposal(db, proposal_id, comment=comment)
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail=f"提案不存在：{proposal_id}")
    audit.record(
        db,
        action="reject_proposal",
        target=f"proposal:{proposal_id}",
        status="ok" if result.get("status") == "rejected" else "denied",
        detail={"comment": comment},
        request=request,
    )
    db.commit()
    return result


@router.post("/{space_id}/draft")
def draft(
    space_id: int,
    question: str = Query(""),
    corpus: str = Query(""),
    max_types: int = Query(4),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """按一句话/一段语料生成本体草案（只产出提案，不写本体）。"""
    if not corpus.strip():
        from ..core.rag.pipeline import ask

        result = ask(db, space_id, question or "总结这份资料的可建模概念", components=get_components())
        corpus = "\n\n".join(item["text"] for item in result["retrieved"][:5])
        evidence = result["retrieved"][:5]
    else:
        evidence = []
    return service.draft_proposals(
        db,
        space_id,
        corpus=corpus,
        components=get_components(),
        question=question,
        max_types=max_types,
        evidence_from_chunks=evidence,
    )
