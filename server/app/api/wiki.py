"""Wiki 与图谱接口：页面、版本、diff、回滚、编译、图谱四种模式。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.wiki import compiler, graph as graph_service, versions
from ..db import get_db
from ..models import OntologySpace, WikiPage

router = APIRouter(prefix="/api/wiki", tags=["wiki"])


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
@router.get("/pages")
def list_pages(
    space_id: int = Query(..., alias="space"),
    page_type: str = Query(""),
    keyword: str = Query(""),
    only_dirty: bool = Query(False),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = select(WikiPage).where(WikiPage.space_id == space_id)
    if page_type:
        query = query.where(WikiPage.page_type == page_type)
    if only_dirty:
        query = query.where(WikiPage.is_dirty.is_(True))
    rows = db.execute(query.order_by(WikiPage.page_type, WikiPage.slug)).scalars().all()
    if keyword:
        needle = keyword.strip().lower()
        rows = [
            row for row in rows
            if needle in (row.title or "").lower() or needle in (row.body or "").lower()
        ]
    return {
        "pages": [
            {
                "id": row.id,
                "slug": row.slug,
                "page_type": row.page_type,
                "title": row.title,
                "version": row.version,
                "is_dirty": row.is_dirty,
                "is_locked": row.is_locked,
                "size": len(row.body or ""),
                "updated_at": row.updated_at.isoformat(sep=" ", timespec="seconds"),
            }
            for row in rows
        ],
        "count": len(rows),
    }


@router.get("/pages/{page_id}")
def page_detail(page_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    page = db.get(WikiPage, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="页面不存在")
    return {
        "id": page.id,
        "slug": page.slug,
        "page_type": page.page_type,
        "title": page.title,
        "body": page.body,
        "version": page.version,
        "is_dirty": page.is_dirty,
        "is_locked": page.is_locked,
        "revisions": versions.list_revisions(db, page_id),
    }


@router.get("/pages/by-slug/{slug}")
def page_by_slug(slug: str, space_id: int = Query(..., alias="space"), db: Session = Depends(get_db)):
    page = db.execute(
        select(WikiPage).where(WikiPage.space_id == space_id, WikiPage.slug == slug)
    ).scalars().first()
    if page is None:
        raise HTTPException(status_code=404, detail="页面不存在")
    return page_detail(page.id, db)


@router.post("/pages/{page_id}/edit")
def edit_page(page_id: int, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    result = versions.save_manual_edit(
        db,
        page_id,
        payload.get("body", ""),
        author=payload.get("author", "manual"),
        message=payload.get("message", "人工编辑"),
        title=payload.get("title"),
    )
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail="页面不存在")
    return result


@router.post("/pages/{page_id}/lock")
def lock_page(page_id: int, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    locked = bool(payload.get("locked", True))
    result = versions.toggle_lock(db, page_id, locked)
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail="页面不存在")
    return result


@router.get("/pages/{page_id}/revisions")
def revisions(page_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"revisions": versions.list_revisions(db, page_id)}


@router.get("/pages/{page_id}/diff")
def diff(
    page_id: int,
    left: int = Query(...),
    right: int = Query(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = versions.diff_versions(db, page_id, left, right)
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail="版本不存在")
    return result


@router.post("/pages/{page_id}/rollback")
def rollback(page_id: int, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    version = int(payload.get("version", 1))
    result = versions.rollback(db, page_id, version, actor=payload.get("actor", "manual"))
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail="页面不存在")
    if result.get("status") == "version_missing":
        raise HTTPException(status_code=404, detail=f"版本不存在：v{version}")
    return result


@router.post("/compile")
def compile_pages(
    space_id: int = Query(..., alias="space"),
    only_dirty: bool = Query(False),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if db.get(OntologySpace, space_id) is None:
        raise HTTPException(status_code=404, detail="空间不存在")
    return compiler.compile_space(db, space_id, only_dirty=only_dirty, message="手动触发编译")


# --------------------------------------------------------------------------- #
# 图谱
# --------------------------------------------------------------------------- #
@router.get("/graph")
def graph_data(
    space_id: int = Query(..., alias="space"),
    layout: bool = Query(True),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    graph = graph_service.build_graph(db, space_id)
    if layout:
        graph = graph_service.layout(graph)
    return graph


@router.get("/graph.svg")
def graph_svg(space_id: int = Query(..., alias="space"), db: Session = Depends(get_db)):
    graph = graph_service.layout(graph_service.build_graph(db, space_id))
    return PlainTextResponse(graph_service.to_svg(graph), media_type="image/svg+xml")


@router.get("/subgraph")
def subgraph(
    space_id: int = Query(..., alias="space"),
    node: str = Query(...),
    depth: int = Query(1),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    graph = graph_service.layout(graph_service.build_graph(db, space_id))
    return graph_service.subgraph(graph, node, depth)


@router.get("/path")
def path(
    space_id: int = Query(..., alias="space"),
    source: str = Query(..., alias="from"),
    target: str = Query(..., alias="to"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    graph = graph_service.build_graph(db, space_id)
    return graph_service.shortest_path(graph, source, target)


@router.get("/insights")
def insights(space_id: int = Query(..., alias="space"), db: Session = Depends(get_db)) -> dict[str, Any]:
    graph = graph_service.build_graph(db, space_id)
    return graph_service.insights(db, space_id, graph)
