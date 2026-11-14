"""文档入库：解析 → 切块 → 嵌入 → 双索引。

幂等性：同一空间内同 ``sha256`` 的文档不会重复入库（返回 ``duplicate``），
这样领域包安装可以反复执行，示例语料不会被灌成几十份。

事务边界：向量写入走独立的 ``session_scope``，所以在调用向量库之前必须
先提交文档与片段，否则 SQLite 会因两个写事务竞争而报 ``database is locked``。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import delete, select

from ...models import Document, DocumentChunk
from ..rag.chunking import chunk_text
from ..rag.retriever import invalidate_cache
from ..text import slugify


@dataclass(slots=True)
class IngestResult:
    status: str
    document_id: int | None = None
    title: str = ""
    chunks: int = 0
    chars: int = 0
    pages: int = 0
    warnings: list[str] | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "document_id": self.document_id,
            "title": self.title,
            "chunks": self.chunks,
            "chars": self.chars,
            "pages": self.pages,
            "warnings": self.warnings or [],
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ingest_bytes(
    session,
    space_id: int,
    filename: str,
    data: bytes,
    *,
    components,
    title: str = "",
    doc_type: str = "制度",
    issuer: str = "",
    jurisdiction: str = "",
    authority: int = 3,
    language: str = "zh",
    source_uri: str = "",
    chunk_size: int = 420,
    chunk_overlap: int = 60,
) -> IngestResult:
    digest = _sha256(data)
    existing = session.execute(
        select(Document).where(Document.space_id == space_id, Document.sha256 == digest)
    ).scalars().first()
    if existing is not None:
        return IngestResult(
            status="duplicate",
            document_id=existing.id,
            title=existing.title,
            chunks=0,
            warnings=["相同哈希的文档已存在，跳过入库"],
        )

    parsed = components.doc_parser.parse(filename, data)
    if not parsed.text.strip():
        return IngestResult(
            status="empty",
            title=title or filename,
            warnings=(parsed.warnings or []) + ["解析结果为空"],
        )

    object_key = f"spaces/{space_id}/{digest[:12]}/{slugify(filename, fallback='doc')}{_ext(filename)}"
    try:
        components.object_store.put(object_key, data, parsed.mime)
    except Exception:  # noqa: BLE001 - 对象存储失败不应阻断入库
        object_key = ""

    document = Document(
        space_id=space_id,
        title=title or _strip_ext(filename),
        filename=filename,
        mime=parsed.mime,
        size_bytes=len(data),
        sha256=digest,
        language=language,
        doc_type=doc_type,
        issuer=issuer,
        jurisdiction=jurisdiction,
        authority=int(authority),
        source_uri=source_uri,
        object_key=object_key,
        page_count=parsed.page_count,
        status="indexing",
    )
    session.add(document)
    session.flush()

    pieces = chunk_text(parsed.text, size=chunk_size, overlap=chunk_overlap)
    chunk_records = [
        DocumentChunk(
            document_id=document.id,
            ordinal=piece.ordinal,
            text=piece.text,
            token_count=piece.token_count,
            locator=piece.locator,
            meta={"page": _page_for(parsed.pages, piece.ordinal)},
        )
        for piece in pieces
    ]
    session.add_all(chunk_records)
    session.commit()  # 先落库，避免与向量写入抢锁

    vectors = components.embedding.embed([record.text for record in chunk_records])
    components.vector_store.upsert(
        [(record.id, vector) for record, vector in zip(chunk_records, vectors)]
    )

    document.status = "indexed"
    session.commit()
    invalidate_cache(space_id)

    return IngestResult(
        status="indexed",
        document_id=document.id,
        title=document.title,
        chunks=len(chunk_records),
        chars=len(parsed.text),
        pages=parsed.page_count,
        warnings=parsed.warnings,
    )


def ingest_text(
    session,
    space_id: int,
    text: str,
    *,
    components,
    title: str,
    **kwargs,
) -> IngestResult:
    filename = f"{slugify(title, fallback='note')}.md"
    return ingest_bytes(
        session,
        space_id,
        filename,
        text.encode("utf-8"),
        components=components,
        title=title,
        **kwargs,
    )


def rebuild_document_index(session, document_id: int, *, components, chunk_size: int = 420, chunk_overlap: int = 60) -> dict:
    """换 embedding 模型后逐篇重建（维度与语义空间都变了，不能混用旧向量）。"""
    document = session.get(Document, document_id)
    if document is None:
        return {"status": "missing", "document_id": document_id}
    chunks = session.execute(
        select(DocumentChunk).where(DocumentChunk.document_id == document_id).order_by(DocumentChunk.ordinal)
    ).scalars().all()
    if not chunks:
        return {"status": "empty", "document_id": document_id}

    old_ids = [chunk.id for chunk in chunks]
    components.vector_store.delete(old_ids)

    vectors = components.embedding.embed([chunk.text for chunk in chunks])
    components.vector_store.upsert([(chunk.id, vector) for chunk, vector in zip(chunks, vectors)])
    document.status = "indexed"
    session.commit()
    invalidate_cache(document.space_id)
    return {"status": "reindexed", "document_id": document_id, "chunks": len(chunks)}


def rebuild_space_index(session, space_id: int, *, components) -> dict:
    documents = session.execute(
        select(Document).where(Document.space_id == space_id)
    ).scalars().all()
    total = 0
    for document in documents:
        result = rebuild_document_index(session, document.id, components=components)
        total += int(result.get("chunks", 0) or 0)
    return {"status": "reindexed", "documents": len(documents), "chunks": total}


def delete_document(session, document_id: int, *, components) -> dict:
    document = session.get(Document, document_id)
    if document is None:
        return {"status": "missing", "document_id": document_id}
    space_id = document.space_id
    chunk_ids = [
        row[0]
        for row in session.execute(
            select(DocumentChunk.id).where(DocumentChunk.document_id == document_id)
        ).all()
    ]
    session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
    session.delete(document)
    session.commit()
    if chunk_ids:
        components.vector_store.delete(chunk_ids)
    invalidate_cache(space_id)
    return {"status": "deleted", "document_id": document_id, "chunks": len(chunk_ids)}


# --------------------------------------------------------------------------- #
def _ext(filename: str) -> str:
    name = filename or ""
    return name[name.rfind(".") :] if "." in name else ""


def _strip_ext(filename: str) -> str:
    name = filename or "未命名文档"
    return name[: name.rfind(".")] if "." in name else name


def _page_for(pages: list[str], ordinal: int) -> int:
    """粗略的页码归属：多页文档按顺序均分，单页恒为 1。"""
    if not pages:
        return 1
    return min(len(pages), 1 + ordinal * len(pages) // max(1, len(pages) * 4))
