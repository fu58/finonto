"""Wiki 接口：页面列表/详情、人工编辑与锁、版本历史/diff/回滚、增量编译、知识图谱。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..core.wiki.compiler import WikiCompiler
from ..core.wiki.graph import GraphService
from ..core.wiki.versions import VersionService
from ..db import get_session
from ..models import WikiPage

router = APIRouter(prefix="/api/wiki", tags=["wiki"])


def _page(session: Session, page_id: int) -> WikiPage:
    page = session.get(WikiPage, page_id)
    if page is None:
        raise HTTPException(404, f"页面 {page_id} 不存在")
    return page


# ------------------------------------------------------------------ 页面
@router.get("/pages")
def pages(space_id: int = 1, session: Session = Depends(get_session)) -> dict[str, Any]:
    compiler = WikiCompiler(session)
    rows = compiler.list_pages(space_id)
    return {
        "total": len(rows),
        "pages": [
            {
                "id": page.id,
                "slug": page.slug,
                "title": page.title,
                "page_type": page.page_type,
                "object_type_id": page.object_type_id,
                "current_version": page.current_version,
                "locked_by_human": page.locked_by_human,
                "dirty": page.dirty,
                "revision_count": len(page.revisions),
                "updated_at": page.updated_at.isoformat() if page.updated_at else "",
            }
            for page in rows
        ],
    }


@router.get("/pages/{page_id}")
def page_detail(page_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    return _detail(session, _page(session, page_id))


@router.get("/by-slug")
def page_by_slug(slug: str, space_id: int = 1, session: Session = Depends(get_session)) -> dict[str, Any]:
    for page in WikiCompiler(session).list_pages(space_id):
        if page.slug == slug:
            return _detail(session, page)
    raise HTTPException(404, f"slug {slug} 不存在")


def _detail(session: Session, page: WikiPage) -> dict[str, Any]:
    view = WikiCompiler(session).page_view(page)
    view["revisions"] = VersionService(session).revisions(page.id)
    return view


@router.post("/spaces/{space_id}/compile")
def compile_space(
    space_id: int,
    only_dirty: bool = False,
    force: bool = False,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """重编译。only_dirty 走增量；force 忽略人工锁。"""
    try:
        report = WikiCompiler(session).compile_space(space_id, only_dirty=only_dirty, force=force)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "created": report.created,
        "updated": report.updated,
        "unchanged": report.unchanged,
        "skipped_locked": report.skipped_locked,
        "pages": report.pages,
    }


# ------------------------------------------------------------------ 人工编辑
@router.put("/pages/{page_id}")
async def edit_page(page_id: int, body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    body_md = str(body.get("body_md", ""))
    if not body_md.strip():
        raise HTTPException(400, "body_md 不能为空")
    try:
        result = WikiCompiler(session).manual_edit(
            page_id,
            body_md,
            note=str(body.get("note", "")),
            author=str(body.get("author", "human")),
            lock=bool(body.get("lock", True)),
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    if result["revision"] is None:
        session.commit()
        return {"changed": False, **result}
    session.commit()
    return {"changed": True, **result}


@router.post("/pages/{page_id}/lock")
def set_lock(page_id: int, locked: bool = True, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        return WikiCompiler(session).set_lock(page_id, locked)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


# ------------------------------------------------------------------ 版本
@router.get("/pages/{page_id}/revisions")
def revisions(page_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    _page(session, page_id)
    return {"page_id": page_id, "revisions": VersionService(session).revisions(page_id)}


@router.get("/pages/{page_id}/revisions/{version_no}")
def revision_detail(page_id: int, version_no: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    revision = VersionService(session).get_revision(page_id, version_no)
    if revision is None:
        raise HTTPException(404, f"版本 v{version_no} 不存在")
    return {
        "id": revision.id,
        "page_id": page_id,
        "version_no": revision.version_no,
        "author": revision.author,
        "change_note": revision.change_note,
        "summary": revision.summary,
        "body_md": revision.body_md,
        "created_at": revision.created_at.isoformat() if revision.created_at else "",
    }


@router.get("/pages/{page_id}/diff")
def diff(page_id: int, left: int, right: int | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        return VersionService(session).diff(page_id, left, right)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/pages/{page_id}/rollback")
def rollback(page_id: int, body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    """回滚是非破坏式的：不删除历史，而是把目标版本内容追加为新版本。"""
    if body.get("to_version") is None:
        raise HTTPException(400, "to_version 必填")
    try:
        result = VersionService(session).rollback(
            page_id,
            int(body["to_version"]),
            actor=str(body.get("actor", "human")),
            note=str(body.get("note", "")),
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return result


@router.delete("/pages/{page_id}/revisions/{version_no}")
def delete_revision(page_id: int, version_no: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        removed = VersionService(session).delete_revision(page_id, version_no)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not removed:
        raise HTTPException(404, f"版本 v{version_no} 不存在")
    return {"removed": version_no}


# ------------------------------------------------------------------ 图谱
@router.get("/graph")
def graph(
    space_id: int = 1,
    include_properties: bool = False,
    include_docs: bool = False,
    domain: str | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return GraphService(session).build(
        space_id,
        include_properties=include_properties,
        include_docs=include_docs,
        domain=domain,
        status=[s for s in status.split(",") if s] if status else None,
    )


@router.get("/graph/subgraph")
def subgraph(
    center_type_id: int,
    space_id: int = 1,
    depth: int = 2,
    include_properties: bool = False,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if depth < 1 or depth > 6:
        raise HTTPException(400, "depth 需在 1~6 之间")
    return GraphService(session).subgraph(
        space_id, center_type_id, depth=depth, include_properties=include_properties
    )


@router.get("/graph/path")
def graph_path(
    start_type_id: int,
    end_type_id: int,
    space_id: int = 1,
    max_hops: int = 6,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return GraphService(session).path(space_id, start_type_id, end_type_id, max_hops=max_hops)


@router.get("/graph/insights")
def graph_insights(space_id: int = 1, session: Session = Depends(get_session)) -> dict[str, Any]:
    return GraphService(session).insights(space_id)
