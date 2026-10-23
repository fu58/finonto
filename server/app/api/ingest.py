"""知识库文档：上传解析、分块预览、列表、删除、重新索引。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.rag.ingest import IngestService, namespace_for
from ..db import get_session
from ..models import Chunk, DocumentRecord
from ..providers.registry import registry

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _doc_view(doc: DocumentRecord, chunk_count: int) -> dict[str, Any]:
    return {
        "id": doc.id,
        "space_id": doc.space_id,
        "name": doc.name,
        "doc_type": doc.doc_type,
        "doc_format": doc.doc_format,
        "media_type": doc.media_type,
        "issuer": doc.issuer,
        "jurisdiction": doc.jurisdiction,
        "effective_date": doc.effective_date,
        "authority": doc.authority,
        "status": doc.status,
        "error": doc.error,
        "char_count": doc.char_count,
        "chunks": chunk_count,
        "storage_key": doc.storage_key,
        "source_uri": doc.source_uri,
        "created_at": doc.created_at.isoformat() if doc.created_at else "",
        "meta": doc.meta,
    }


@router.get("")
def list_documents(
    session: Session = Depends(get_session), space_id: int | None = None, limit: int = 200
) -> dict[str, Any]:
    stmt = select(DocumentRecord).order_by(DocumentRecord.id.desc()).limit(limit)
    if space_id:
        stmt = stmt.where(DocumentRecord.space_id == space_id)
    docs = list(session.scalars(stmt).all())
    counts = dict(
        session.execute(select(Chunk.doc_id, func.count()).group_by(Chunk.doc_id)).all()
    )
    return {
        "documents": [_doc_view(doc, counts.get(doc.id, 0)) for doc in docs],
        "formats": registry.doc_parser().formats,
        "total_chunks": sum(counts.values()),
    }


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    space_id: int = Form(...),
    doc_type: str = Form(""),
    issuer: str = Form(""),
    jurisdiction: str = Form(""),
    effective_date: str = Form(""),
    authority: float = Form(0.6),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    payload = await file.read()
    if not payload:
        raise HTTPException(400, "文件内容为空")
    report = IngestService(session).ingest_bytes(
        name=file.filename or "untitled",
        payload=payload,
        space_id=space_id,
        doc_format="",
        media_type=file.content_type or "",
        doc_type=doc_type,
        issuer=issuer,
        jurisdiction=jurisdiction,
        effective_date=effective_date,
        authority=authority,
    )
    session.commit()
    doc = session.get(DocumentRecord, report.doc_id)
    return {
        "report": report.__dict__,
        "document": _doc_view(doc, report.chunks) if doc else None,
    }


@router.post("/text")
def ingest_text(body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    text = str(body.get("text", ""))
    if not text.strip():
        raise HTTPException(400, "text 不能为空")
    report = IngestService(session).ingest_bytes(
        name=str(body.get("name", "inline.md")),
        payload=text.encode("utf-8"),
        space_id=int(body.get("space_id", 1)),
        doc_format=str(body.get("doc_format", "md")),
        doc_type=str(body.get("doc_type", "手工录入")),
        authority=float(body.get("authority", 0.5)),
    )
    session.commit()
    doc = session.get(DocumentRecord, report.doc_id)
    return {
        "report": report.__dict__,
        "document": _doc_view(doc, report.chunks) if doc else None,
    }


@router.get("/{doc_id}/chunks")
def list_chunks(doc_id: int, session: Session = Depends(get_session), limit: int = 200) -> dict[str, Any]:
    rows = list(
        session.scalars(
            select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.ordinal).limit(limit)
        ).all()
    )
    doc = session.get(DocumentRecord, doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    return {
        "document": _doc_view(doc, len(rows)),
        "chunks": [
            {
                "id": chunk.id,
                "ordinal": chunk.ordinal,
                "locator": chunk.locator,
                "token_estimate": chunk.token_estimate,
                "text": chunk.text,
                "meta": chunk.meta,
            }
            for chunk in rows
        ],
    }


@router.post("/preview-chunks")
def preview_chunks(body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    """按当前分块参数预览切块结果，用于调 chunk_max_tokens 时不写入数据库。"""
    text = str(body.get("text", ""))
    if not text.strip():
        raise HTTPException(400, "text 不能为空")
    return {"chunks": IngestService(session).preview_chunks(text, max_tokens=body.get("max_tokens"))}


@router.post("/{doc_id}/reindex")
def reindex(doc_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    doc = session.get(DocumentRecord, doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    rows = list(session.scalars(select(Chunk).where(Chunk.doc_id == doc_id)).all())
    embedded = IngestService(session).embed_and_index(rows, doc.space_id)
    rebuilt = IngestService(session).rebuild_lexical_index(doc.space_id)
    doc.status = "indexed" if rows else "empty"
    session.commit()
    return {"embedded": embedded, "lexical_documents": rebuilt, "namespace": namespace_for(doc.space_id)}


@router.delete("/{doc_id}")
def delete_document(doc_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    if session.get(DocumentRecord, doc_id) is None:
        raise HTTPException(404, "文档不存在")
    IngestService(session).delete_document(doc_id)
    session.commit()
    return {"deleted": doc_id}
