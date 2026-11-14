"""文档入库接口。

上传走 **原始请求体**（`application/octet-stream`）+ query 参数传文件名：
这样既不需要 python-multipart，前端也只需要一行 ``fetch(url, {body: file})``。
需要 multipart 的调用方（curl -F）由页面层的表单处理器兜底（见 web/pages.py）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.rag.ingest import (
    delete_document,
    ingest_bytes,
    ingest_text,
    rebuild_document_index,
    rebuild_space_index,
)
from ..db import get_db
from ..models import Document, DocumentChunk, OntologySpace
from ..providers.registry import get_components

router = APIRouter(prefix="/api", tags=["documents"])


@router.get("/documents")
def list_documents(
    space_id: int = Query(..., alias="space"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = db.execute(
        select(Document).where(Document.space_id == space_id).order_by(Document.id.desc())
    ).scalars().all()
    result = []
    for row in rows:
        chunk_count = int(
            db.execute(
                select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == row.id)
            ).scalar_one()
        )
        result.append(
            {
                "id": row.id,
                "title": row.title,
                "filename": row.filename,
                "mime": row.mime,
                "doc_type": row.doc_type,
                "issuer": row.issuer,
                "jurisdiction": row.jurisdiction,
                "authority": row.authority,
                "size_bytes": row.size_bytes,
                "page_count": row.page_count,
                "chunks": chunk_count,
                "status": row.status,
                "created_at": row.created_at.isoformat(sep=" ", timespec="seconds"),
            }
        )
    return {"documents": result, "count": len(result)}


@router.get("/documents/{document_id}/chunks")
def chunks(document_id: int, limit: int = Query(200), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.ordinal)
        .limit(limit)
    ).scalars().all()
    return {
        "chunks": [
            {"id": row.id, "ordinal": row.ordinal, "locator": row.locator,
             "tokens": row.token_count, "text": row.text}
            for row in rows
        ]
    }


@router.post("/documents/upload")
async def upload(
    request: Request,
    space_id: int = Query(..., alias="space"),
    filename: str = Query(...),
    title: str = Query(""),
    doc_type: str = Query("制度"),
    issuer: str = Query(""),
    jurisdiction: str = Query(""),
    authority: int = Query(3),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = get_settings()
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="请求体为空")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"文件超过 {settings.max_upload_mb}MB 上限")
    if db.get(OntologySpace, space_id) is None:
        raise HTTPException(status_code=404, detail="空间不存在")

    result = ingest_bytes(
        db,
        space_id,
        filename,
        data,
        components=get_components(),
        title=title or filename,
        doc_type=doc_type,
        issuer=issuer,
        jurisdiction=jurisdiction,
        authority=authority,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )
    return result.as_dict()


@router.post("/documents/text")
def upload_text(
    space_id: int = Query(..., alias="space"),
    title: str = Query("未命名片段"),
    content: str = Query(""),
    doc_type: str = Query("制度"),
    authority: int = Query(3),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not content.strip():
        raise HTTPException(status_code=400, detail="content 不能为空")
    settings = get_settings()
    result = ingest_text(
        db,
        space_id,
        content,
        components=get_components(),
        title=title,
        doc_type=doc_type,
        authority=authority,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )
    return result.as_dict()


@router.post("/documents/{document_id}/reindex")
def reindex(document_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = rebuild_document_index(db, document_id, components=get_components())
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail="文档不存在")
    return result


@router.post("/documents/reindex")
def reindex_space(space_id: int = Query(..., alias="space"), db: Session = Depends(get_db)) -> dict[str, Any]:
    return rebuild_space_index(db, space_id, components=get_components())


@router.delete("/documents/{document_id}")
def remove(document_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return delete_document(db, document_id, components=get_components())


@router.get("/objects/{key:path}")
def get_object(key: str, db: Session = Depends(get_db)) -> Response:
    components = get_components()
    try:
        data = components.object_store.get(key)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="对象不存在")
    return Response(content=data, media_type="application/octet-stream")


@router.get("/documents/stats")
def document_stats(space_id: int = Query(..., alias="space"), db: Session = Depends(get_db)) -> dict[str, Any]:
    documents = int(
        db.execute(select(func.count(Document.id)).where(Document.space_id == space_id)).scalar_one()
    )
    chunks = int(
        db.execute(
            select(func.count(DocumentChunk.id))
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(Document.space_id == space_id)
        ).scalar_one()
    )
    chars = db.execute(
        select(func.coalesce(func.sum(func.length(DocumentChunk.text)), 0))
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.space_id == space_id)
    ).scalar_one()
    return {
        "documents": documents,
        "chunks": chunks,
        "chars": int(chars or 0),
        "vectors": get_components().vector_store.stats(),
    }
