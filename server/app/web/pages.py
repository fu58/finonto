"""页面路由：服务端渲染 + 表单 POST（无 JS 也能用）。

所有写操作都返回 303 重定向（Post/Redirect/Get），避免刷新重复提交；
交互增强由 static/js/ui.js 在浏览器端接管（data-post / data-get / data-confirm）。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.agent import mcp_client, memory as memory_service, skills
from ..core.ontology import owl, packs, service
from ..core.wiki import compiler as wiki_compiler
from ..core.wiki import graph as graph_service
from ..core.wiki import versions as wiki_versions
from ..db import get_db
from ..models import AgentRun, Document, OntologySpace, WikiPage
from ..providers.registry import catalog as component_catalog
from ..providers.registry import get_components
from ..config import get_settings
from ..middleware import SESSION_COOKIE
from ..security import client_ip, has_role, role_label, user_agent
from ..core import audit, auth_service, backup as backup_service, diagnostics
from .render import page_context, render, render_error

router = APIRouter(tags=["pages"])


# --------------------------------------------------------------------------- #
# 页面级权限
# --------------------------------------------------------------------------- #
def deny(request: Request, minimum: str):
    """检查页面访问权限：返回 None 表示放行，否则返回该直接返回的响应。"""
    settings = get_settings()
    if not settings.auth_enabled:
        return None
    user = getattr(request.state, "user", None)
    if user is None:
        return redirect("/login")
    if not has_role(getattr(user, "role", ""), minimum):
        audit.record(
            action="login_failed",
            target=str(request.url.path),
            status="denied",
            detail={"required": minimum, "actual": getattr(user, "role", "")},
            request=request,
        )
        return render_error(request, 403, "权限不足", f"该页面需要 {minimum} 及以上角色")
    return None


async def form_data(request: Request) -> dict[str, str]:
    """自解析 urlencoded 表单（避免引入 python-multipart）。"""
    body = await request.body()
    if not body:
        return {}
    content_type = request.headers.get("content-type", "")
    if "x-www-form-urlencoded" not in content_type:
        return {}
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[0] if values else "" for key, values in parsed.items()}


def redirect(path: str, space_id: int | None = None) -> RedirectResponse:
    if space_id:
        joiner = "&" if "?" in path else "?"
        path = f"{path}{joiner}space={space_id}"
    return RedirectResponse(path, status_code=303)


def space_of(request: Request) -> int:
    raw = request.query_params.get("space", "0")
    try:
        return int(raw)
    except ValueError:
        return 0


# --------------------------------------------------------------------------- #
# 问答
# --------------------------------------------------------------------------- #
@router.get("/")
def page_chat(request: Request, db: Session = Depends(get_db)):
    context = page_context(
        db,
        request=request,
        space_id=space_of(request),
        active="chat",
        current_path="/",
        question=request.query_params.get("q", ""),
    )
    return render("chat.html", context)


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
@router.get("/agent")
def page_agent(request: Request, db: Session = Depends(get_db)):
    space_id = space_of(request)
    runs = db.execute(
        select(AgentRun).where(AgentRun.space_id == space_id).order_by(AgentRun.id.desc()).limit(12)
    ).scalars().all()
    mcp_client.ensure_builtin_servers(db)
    context = page_context(
        db,
        request=request,
        space_id=space_id,
        active="agent",
        current_path="/agent",
        skills=skills.catalog(),
        runs=[
            {
                "id": row.id,
                "skill": row.skill,
                "goal": row.goal,
                "status": row.status,
                "steps": row.step_count,
                "tokens": row.token_count,
                "proposals": row.proposal_ids or [],
            }
            for row in runs
        ],
        memories=memory_service.list_memories(db, space_id),
        memory_stats=memory_service.stats(db, space_id),
        mcp_servers=mcp_client.list_servers(db),
    )
    return render("agent.html", context)


# --------------------------------------------------------------------------- #
# 本体工作室
# --------------------------------------------------------------------------- #
@router.get("/ontology")
def page_ontology(request: Request, db: Session = Depends(get_db)):
    space_id = space_of(request)
    keyword = request.query_params.get("keyword", "")
    domain = request.query_params.get("domain", "")
    kind = request.query_params.get("kind", "")
    types = service.list_types(db, space_id, keyword=keyword, business_domain=domain, kind=kind)
    detail = None
    raw_type = request.query_params.get("type", "")
    if raw_type.isdigit():
        detail = service.type_detail(db, int(raw_type))
    context = page_context(
        db,
        request=request,
        space_id=space_id,
        active="ontology",
        current_path="/ontology",
        types=types,
        detail=detail,
        keyword=keyword,
        domain=domain,
        kind=kind,
    )
    return render("ontology.html", context)


def _parse_properties(raw: str) -> list[dict[str, Any]]:
    """把「名称:类型[:必填]」多行文本转成属性列表。"""
    items: list[dict[str, Any]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.replace("：", ":").split(":")]
        if not parts or not parts[0]:
            continue
        spec: dict[str, Any] = {"label": parts[0], "data_type": "string"}
        if len(parts) > 1 and parts[1]:
            spec["data_type"] = parts[1]
        if len(parts) > 2:
            spec["required"] = parts[2].lower() in ("1", "true", "yes", "必填")
        items.append(spec)
    return items


@router.post("/ontology/create")
async def create_type_page(request: Request, db: Session = Depends(get_db)):
    space_id = space_of(request)
    data = await form_data(request)
    payload = {
        "label": data.get("label", "").strip(),
        "definition": data.get("definition", "").strip(),
        "kind": data.get("kind", "class"),
        "business_domain": data.get("business_domain", ""),
        "properties": _parse_properties(data.get("properties", "")),
    }
    result = service.create_type_manual(db, space_id, payload)
    audit.record(
        db,
        action="create_type",
        target=payload.get("label", ""),
        status="ok" if result.get("status") == "created" else "failed",
        detail={"result": result.get("status"), "type_id": result.get("type_id")},
        request=request,
    )
    db.commit()
    return redirect("/ontology", space_id)


@router.post("/ontology/{type_id}/definition")
async def update_type_page(type_id: int, request: Request, db: Session = Depends(get_db)):
    space_id = space_of(request)
    data = await form_data(request)
    result = service.update_type_definition(
        db,
        type_id,
        {
            "definition": data.get("definition", ""),
            "business_domain": data.get("business_domain", ""),
            "kind": data.get("kind", ""),
        },
    )
    audit.record(
        db,
        action="update_type",
        target=f"type:{type_id}",
        status="ok" if result.get("status") == "updated" else "denied",
        detail={"version": result.get("version")},
        request=request,
    )
    db.commit()
    return redirect(f"/ontology?type={type_id}", space_id)


@router.post("/ontology/{type_id}/delete")
def delete_type_page(type_id: int, request: Request, db: Session = Depends(get_db)):
    result = service.delete_type(db, type_id)
    audit.record(
        db,
        action="delete_type",
        target=f"type:{type_id}",
        status="ok" if result.get("status") == "deleted" else "failed",
        request=request,
    )
    db.commit()
    return redirect("/ontology", space_of(request))


# --------------------------------------------------------------------------- #
# 提案
# --------------------------------------------------------------------------- #
@router.get("/proposals")
def page_proposals(request: Request, db: Session = Depends(get_db)):
    space_id = space_of(request)
    status = request.query_params.get("status", "")
    proposals = service.list_proposals(db, space_id, status=status or None)
    detailed = []
    for item in proposals:
        detail = service.proposal_detail(db, item["id"]) or {}
        detailed.append({**item, "payload": detail.get("payload", {}), "evidence": detail.get("evidence", [])})
    context = page_context(
        db,
        request=request,
        space_id=space_id,
        active="proposals",
        current_path="/proposals",
        proposals=detailed,
        status=status,
    )
    return render("proposals.html", context)


@router.post("/proposals/{proposal_id}/accept")
def accept_page(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    space_id = space_of(request)
    result = service.accept_proposal(
        db, proposal_id, components=get_components(), settings=get_settings()
    )
    audit.record(
        db,
        action="accept_proposal",
        target=f"proposal:{proposal_id}",
        status="ok" if result.get("status") == "accepted" else "denied",
        detail={
            "result": result.get("status"),
            "label": result.get("label", ""),
            "type_id": result.get("type_id"),
        },
        request=request,
    )
    db.commit()
    return redirect("/proposals", space_id)


@router.post("/proposals/{proposal_id}/reject")
def reject_page(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    space_id = space_of(request)
    result = service.reject_proposal(db, proposal_id, comment="页面操作驳回")
    audit.record(
        db,
        action="reject_proposal",
        target=f"proposal:{proposal_id}",
        status="ok" if result.get("status") == "rejected" else "denied",
        request=request,
    )
    db.commit()
    return redirect("/proposals", space_id)


# --------------------------------------------------------------------------- #
# Wiki
# --------------------------------------------------------------------------- #
@router.get("/wiki")
def page_wiki(request: Request, db: Session = Depends(get_db)):
    space_id = space_of(request)
    keyword = request.query_params.get("keyword", "")
    page_type = request.query_params.get("page_type", "")
    only_dirty = request.query_params.get("dirty") == "1"

    query = select(WikiPage).where(WikiPage.space_id == space_id)
    if page_type:
        query = query.where(WikiPage.page_type == page_type)
    if only_dirty:
        query = query.where(WikiPage.is_dirty.is_(True))
    pages = db.execute(query.order_by(WikiPage.page_type, WikiPage.slug)).scalars().all()
    if keyword:
        needle = keyword.lower()
        pages = [p for p in pages if needle in (p.title or "").lower() or needle in (p.body or "").lower()]

    page = None
    revisions: list[dict] = []
    diff = None
    raw_page = request.query_params.get("page", "")
    if raw_page.isdigit():
        page = db.get(WikiPage, int(raw_page))
    elif pages:
        page = pages[0]

    if page is not None:
        revisions = wiki_versions.list_revisions(db, page.id)
        left = request.query_params.get("left", "")
        right = request.query_params.get("right", "")
        if left.isdigit() and right.isdigit():
            diff = wiki_versions.diff_versions(db, page.id, int(left), int(right))
        rev = request.query_params.get("rev", "")
        if rev.isdigit():
            target = wiki_versions.get_revision(db, page.id, int(rev))
            if target is not None:
                page = _RevisionView(page, target)
                revisions = wiki_versions.list_revisions(db, page.id)

    page_titles = _title_resolver(db, space_id)
    context = page_context(
        db,
        request=request,
        space_id=space_id,
        active="wiki",
        current_path="/wiki",
        pages=[
            {
                "id": item.id,
                "slug": item.slug,
                "page_type": item.page_type,
                "title": item.title,
                "version": item.version,
                "is_dirty": item.is_dirty,
                "is_locked": item.is_locked,
            }
            for item in pages
        ],
        page=page,
        revisions=revisions,
        diff=diff,
        keyword=keyword,
        page_type=page_type,
        only_dirty=only_dirty,
        page_titles=page_titles,
    )
    return render("wiki.html", context)


class _RevisionView:
    """把某个历史版本包装成页面对象，供模板复用（只读视图）。"""

    def __init__(self, page: WikiPage, revision) -> None:
        self.id = page.id
        self.slug = page.slug
        self.page_type = page.page_type
        self.title = revision.title or page.title
        self.body = revision.body
        self.version = revision.version
        self.is_dirty = False
        self.is_locked = page.is_locked
        self.viewing_revision = True


def _title_resolver(db: Session, space_id: int):
    cache: dict[str, str] = {}

    def resolve(slug: str) -> str:
        if slug in cache:
            return cache[slug]
        page = db.execute(
            select(WikiPage).where(WikiPage.space_id == space_id, WikiPage.slug == slug)
        ).scalars().first()
        title = page.title if page else slug
        cache[slug] = title
        return title

    return resolve


@router.post("/wiki/compile")
def compile_wiki(request: Request, db: Session = Depends(get_db)):
    space_id = space_of(request)
    only_dirty = request.query_params.get("dirty") == "1"
    stats = wiki_compiler.compile_space(db, space_id, only_dirty=only_dirty, message="手动编译")
    audit.record(
        db,
        action="compile_wiki",
        target=f"space:{space_id}",
        detail={"only_dirty": only_dirty, "pages": stats.get("pages"), "updated": stats.get("updated")},
        request=request,
    )
    db.commit()
    return redirect("/wiki", space_id)


@router.post("/wiki/{page_id}/edit")
async def edit_wiki(page_id: int, request: Request, db: Session = Depends(get_db)):
    space_id = space_of(request)
    data = await form_data(request)
    body = data.get("body", "")
    result = wiki_versions.save_manual_edit(
        db, page_id, body, author="manual", message="人工编辑", title=data.get("title")
    )
    audit.record(
        db,
        action="edit_wiki",
        target=f"page:{page_id}",
        status="ok" if result.get("status") == "saved" else "denied",
        detail={"version": result.get("version"), "chars": len(body)},
        request=request,
    )
    db.commit()
    return redirect(f"/wiki?page={page_id}", space_id)


@router.post("/wiki/{page_id}/lock")
async def lock_wiki(page_id: int, request: Request, db: Session = Depends(get_db)):
    space_id = space_of(request)
    data = await form_data(request)
    locked = data.get("locked", "true").lower() in ("1", "true", "yes")
    result = wiki_versions.toggle_lock(db, page_id, locked)
    audit.record(
        db,
        action="lock_wiki",
        target=f"page:{page_id}",
        status="ok" if result.get("status") in ("locked", "unlocked") else "failed",
        detail={"locked": locked},
        request=request,
    )
    db.commit()
    return redirect(f"/wiki?page={page_id}", space_id)


@router.post("/wiki/{page_id}/rollback")
async def rollback_wiki(page_id: int, request: Request, db: Session = Depends(get_db)):
    space_id = space_of(request)
    data = await form_data(request)
    version = data.get("version", "1")
    if version.isdigit():
        result = wiki_versions.rollback(db, page_id, int(version))
        audit.record(
            db,
            action="rollback_wiki",
            target=f"page:{page_id}",
            status="ok" if result.get("status") == "rolled_back" else "denied",
            detail={"target_version": int(version), "new_version": result.get("new_version")},
            request=request,
        )
        db.commit()
    return redirect(f"/wiki?page={page_id}", space_id)


# --------------------------------------------------------------------------- #
# 图谱
# --------------------------------------------------------------------------- #
@router.get("/graph")
def page_graph(request: Request, db: Session = Depends(get_db)):
    space_id = space_of(request)
    mode = request.query_params.get("mode", "full")
    node = request.query_params.get("node", "")
    source = request.query_params.get("from", "")
    target = request.query_params.get("to", "")
    depth = request.query_params.get("depth", "1")
    try:
        depth_value = max(1, min(3, int(depth)))
    except ValueError:
        depth_value = 1

    graph = graph_service.build_graph(db, space_id)
    path = None
    focus = ""
    if mode == "neighborhood" and node:
        graph = graph_service.subgraph(graph, node, depth_value)
        focus = node
    elif mode == "path" and source and target:
        path = graph_service.shortest_path(graph, source, target)
    graph = graph_service.layout(graph)
    insights = graph_service.insights(db, space_id, graph_service.build_graph(db, space_id))

    context = page_context(
        db,
        request=request,
        space_id=space_id,
        active="graph",
        current_path="/graph",
        mode=mode,
        focus=focus,
        source=source,
        target=target,
        path=path,
        svg=graph_service.to_svg(graph),
        insights=insights,
    )
    return render("graph.html", context)


# --------------------------------------------------------------------------- #
# 文档
# --------------------------------------------------------------------------- #
@router.get("/documents")
def page_documents(request: Request, db: Session = Depends(get_db)):
    from ..api.ingest import document_stats

    space_id = space_of(request)
    rows = db.execute(
        select(Document).where(Document.space_id == space_id).order_by(Document.id.desc())
    ).scalars().all()
    from sqlalchemy import func

    from ..models import DocumentChunk

    documents = []
    for row in rows:
        chunk_count = int(
            db.execute(
                select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == row.id)
            ).scalar_one()
        )
        documents.append(
            {
                "id": row.id,
                "title": row.title,
                "filename": row.filename,
                "doc_type": row.doc_type,
                "issuer": row.issuer,
                "authority": row.authority,
                "size_bytes": row.size_bytes,
                "chunks": chunk_count,
                "status": row.status,
            }
        )
    stats = document_stats(space_id=space_id, db=db)
    context = page_context(
        db,
        request=request,
        space_id=space_id,
        active="documents",
        current_path="/documents",
        documents=documents,
        stats=stats,
        chunks=None,
        current_doc=None,
    )
    return render("documents.html", context)


@router.get("/documents/{document_id}")
def page_document_chunks(document_id: int, request: Request, db: Session = Depends(get_db)):
    from sqlalchemy import func

    from ..models import DocumentChunk
    from ..api.ingest import document_stats

    space_id = space_of(request)
    document = db.get(Document, document_id)
    rows = db.execute(
        select(Document).where(Document.space_id == space_id).order_by(Document.id.desc())
    ).scalars().all()
    documents = []
    for row in rows:
        chunk_count = int(
            db.execute(
                select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == row.id)
            ).scalar_one()
        )
        documents.append(
            {
                "id": row.id,
                "title": row.title,
                "filename": row.filename,
                "doc_type": row.doc_type,
                "issuer": row.issuer,
                "authority": row.authority,
                "size_bytes": row.size_bytes,
                "chunks": chunk_count,
                "status": row.status,
            }
        )
    chunks = db.execute(
        select(DocumentChunk).where(DocumentChunk.document_id == document_id).order_by(DocumentChunk.ordinal)
    ).scalars().all()
    context = page_context(
        db,
        request=request,
        space_id=space_id,
        active="documents",
        current_path="/documents",
        documents=documents,
        stats=document_stats(space_id=space_id, db=db),
        chunks=[
            {"id": row.id, "ordinal": row.ordinal, "locator": row.locator,
             "tokens": row.token_count, "text": row.text}
            for row in chunks
        ],
        current_doc=document,
    )
    return render("documents.html", context)


@router.post("/documents/text")
async def ingest_text_page(request: Request, db: Session = Depends(get_db)):
    from ..core.rag.ingest import ingest_text

    space_id = space_of(request)
    data = await form_data(request)
    settings = get_settings()
    result = ingest_text(
        db,
        space_id,
        data.get("content", ""),
        components=get_components(),
        title=data.get("title", "未命名片段"),
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )
    audit.record(
        db,
        action="ingest_document",
        target=result.title or data.get("title", ""),
        status="ok" if result.status == "indexed" else result.status,
        detail={"chunks": result.chunks, "chars": result.chars, "document_id": result.document_id},
        request=request,
    )
    db.commit()
    return redirect("/documents", space_id)


@router.post("/documents/{document_id}/reindex")
def reindex_page(document_id: int, request: Request, db: Session = Depends(get_db)):
    from ..core.rag.ingest import rebuild_document_index

    result = rebuild_document_index(db, document_id, components=get_components())
    audit.record(
        db,
        action="reindex",
        target=f"document:{document_id}",
        status="ok" if result.get("status") == "reindexed" else "failed",
        detail={"chunks": result.get("chunks")},
        request=request,
    )
    db.commit()
    return redirect("/documents", space_of(request))


@router.post("/documents/{document_id}/delete")
def delete_document_page(document_id: int, request: Request, db: Session = Depends(get_db)):
    from ..core.rag.ingest import delete_document

    result = delete_document(db, document_id, components=get_components())
    audit.record(
        db,
        action="delete_document",
        target=f"document:{document_id}",
        status="ok" if result.get("status") == "deleted" else "failed",
        request=request,
    )
    db.commit()
    return redirect("/documents", space_of(request))


@router.post("/documents/reindex")
def reindex_all_page(request: Request, db: Session = Depends(get_db)):
    from ..core.rag.ingest import rebuild_space_index

    space_id = space_of(request)
    result = rebuild_space_index(db, space_id, components=get_components())
    audit.record(
        db,
        action="reindex",
        target=f"space:{space_id}",
        detail={"documents": result.get("documents"), "chunks": result.get("chunks")},
        request=request,
    )
    db.commit()
    return redirect("/documents", space_id)


# --------------------------------------------------------------------------- #
# 领域包
# --------------------------------------------------------------------------- #
@router.get("/packs")
def page_packs(request: Request, db: Session = Depends(get_db)):
    installed = {item["key"]: item for item in service.list_spaces(db)}
    gallery = packs.gallery()
    for item in gallery:
        item["installed"] = item["key"] in installed
        item["space"] = installed.get(item["key"])
    context = page_context(db, request=request, space_id=space_of(request), active="packs", current_path="/packs", gallery=gallery)
    return render("packs.html", context)


@router.post("/packs/{pack_key}/install")
def install_pack_page(pack_key: str, request: Request, db: Session = Depends(get_db)):
    if packs.pack_exists(pack_key):
        result = service.install_pack(
            db, pack_key, components=get_components(), settings=get_settings()
        )
        audit.record(
            db,
            action="install_pack",
            target=pack_key,
            detail={"types": result.get("created_types"), "links": result.get("created_links")},
            request=request,
        )
        db.commit()
    return redirect("/packs", space_of(request))


@router.post("/packs/bootstrap")
async def bootstrap_page(request: Request, db: Session = Depends(get_db)):
    for pack in packs.all_packs():
        service.install_pack(db, pack.key, components=get_components(), settings=get_settings())
    audit.record(
        db,
        action="install_pack",
        target="all",
        detail={"packs": [item.key for item in packs.all_packs()]},
        request=request,
    )
    db.commit()
    return redirect("/packs", space_of(request))


# --------------------------------------------------------------------------- #
# 组件中心
# --------------------------------------------------------------------------- #
@router.get("/components")
def page_components(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    from ..providers.registry import COMPONENT_TO_CATALOG

    categories = []
    for category in component_catalog(settings):
        categories.append(category)
    context = page_context(
        db,
        request=request,
        space_id=space_of(request),
        active="components",
        current_path="/components",
        categories=categories,
        components_file=settings.components_path,
        settings=settings,
    )
    return render("components.html", context)


@router.post("/components/activate")
async def activate_component_page(request: Request, db: Session = Depends(get_db)):
    from ..providers.registry import activate

    data = await form_data(request)
    status = "ok"
    try:
        activate(data.get("category", ""), data.get("key", ""))
    except ValueError:
        status = "failed"
    audit.record(
        db,
        action="activate_component",
        target=f"{data.get('category', '')}={data.get('key', '')}",
        status=status,
        request=request,
    )
    db.commit()
    return redirect("/components", space_of(request))

