"""文档入库服务：解析 -> 切块 -> 嵌入 -> 双索引（BM25 + 向量）。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import Chunk, DocumentRecord
from ...providers.base import ParsedDocument
from ...providers.registry import registry
from ..text import truncate
from .bm25 import index_registry
from .chunking import chunk_blocks, estimate_tokens

NAMESPACE = "chunks"


def namespace_for(space_id: int | None) -> str:
    return f"{NAMESPACE}:space:{space_id or 0}"


@dataclass
class IngestReport:
    doc_id: int
    name: str
    doc_format: str
    chunks: int
    chars: int
    elapsed_ms: int
    warnings: list[str]


class IngestService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.registry = registry

    # ------------------------------------------------------------- 解析
    def parse_payload(self, payload: bytes, doc_format: str, filename: str) -> ParsedDocument:
        parser = self.registry.doc_parser()
        return parser.parse(payload, doc_format, filename)

    def ingest_bytes(
        self,
        *,
        name: str,
        payload: bytes,
        space_id: int | None,
        doc_format: str = "",
        media_type: str = "",
        doc_type: str = "",
        jurisdiction: str = "",
        issuer: str = "",
        effective_date: str = "",
        authority: float = 0.6,
        source_uri: str = "",
        meta: dict[str, Any] | None = None,
    ) -> IngestReport:
        import time

        started = time.perf_counter()
        fmt = (doc_format or name.rsplit(".", 1)[-1] if "." in name else "txt").lower().lstrip(".")
        parser = self.registry.doc_parser()
        warnings: list[str] = []
        if not parser.can_parse(fmt):
            warnings.append(f"文档格式 {fmt} 不在已注册解析器列表内，按纯文本处理")
        parsed = parser.parse(payload, fmt, name)
        if not parsed.blocks:
            warnings.append("解析后没有可用文本块")

        storage_key = f"documents/{space_id or 0}/{hashlib.sha256(payload).hexdigest()[:20]}.{fmt}"
        store = self.registry.object_store()
        try:
            store.put(storage_key, payload, media_type or fmt)
        except Exception as exc:  # noqa: BLE001  存储失败不应阻断入库
            warnings.append(f"对象存储写入失败: {exc}")
            storage_key = ""

        doc = DocumentRecord(
            space_id=space_id,
            name=name,
            source_uri=source_uri,
            storage_key=storage_key,
            media_type=media_type,
            doc_format=fmt,
            doc_type=doc_type or _guess_doc_type(name, parsed.title),
            jurisdiction=jurisdiction,
            issuer=issuer,
            effective_date=effective_date,
            authority=authority,
            status="parsing",
            char_count=len(payload),
            meta={
                **(meta or {}),
                "title": parsed.title,
                "parser": parser.name,
                "parse_meta": parsed.meta,
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
        )
        self.session.add(doc)
        self.session.flush()

        drafts = chunk_blocks(
            parsed.blocks,
            max_tokens=settings.chunk_max_tokens,
            overlap_tokens=settings.chunk_overlap_tokens,
        )
        rows: list[Chunk] = []
        for ordinal, draft in enumerate(drafts):
            rows.append(
                Chunk(
                    doc_id=doc.id,
                    space_id=space_id or 0,
                    ordinal=ordinal,
                    text=draft.text,
                    locator=draft.locator,
                    token_estimate=draft.tokens,
                    meta={
                        **(draft.meta or {}),
                        "page": draft.page,
                        "doc_name": name,
                        "doc_type": doc.doc_type,
                        "jurisdiction": jurisdiction,
                        "authority": authority,
                        "effective_date": effective_date,
                    },
                )
            )
        self.session.add_all(rows)
        self.session.flush()
        report_chunks = self.embed_and_index(rows, space_id or 0)
        doc.status = "indexed" if report_chunks else "empty"
        doc.char_count = sum(len(r.text) for r in rows)
        self.session.flush()
        return IngestReport(
            doc_id=doc.id,
            name=name,
            doc_format=fmt,
            chunks=len(rows),
            chars=doc.char_count,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            warnings=warnings,
        )

    def ingest_file(self, path: Path, **kwargs: Any) -> IngestReport:
        payload = Path(path).read_bytes()
        return self.ingest_bytes(name=Path(path).name, payload=payload, doc_format=kwargs.pop("doc_format", "") or Path(path).suffix.lstrip("."), **kwargs)

    def ingest_text(self, *, name: str, text: str, **kwargs: Any) -> IngestReport:
        return self.ingest_bytes(name=name, payload=text.encode("utf-8"), doc_format="md", media_type="text/markdown", **kwargs)

    # ------------------------------------------------------- 嵌入与索引
    def embed_and_index(self, rows: list[Chunk], space_id: int) -> int:
        if not rows:
            return 0
        embedder = self.registry.embedding()
        vs = self.registry.vector_store()
        texts = [r.text for r in rows]
        if hasattr(embedder, "fit"):
            existing = self.session.scalars(
                select(Chunk.text).where(Chunk.space_id == space_id).limit(4000)
            ).all()
            embedder.fit(list(existing) + texts)
        matrix = embedder.embed(texts)
        vectors = {row.id: matrix[i] for i, row in enumerate(rows)}
        metas = {
            row.id: {
                "space_id": space_id,
                "doc_id": row.doc_id,
                "doc_name": (row.meta or {}).get("doc_name", ""),
                "doc_type": (row.meta or {}).get("doc_type", ""),
                "jurisdiction": (row.meta or {}).get("jurisdiction", ""),
                "authority": (row.meta or {}).get("authority", 0.5),
                "page": (row.meta or {}).get("page"),
                "ordinal": row.ordinal,
            }
            for row in rows
        }
        vs.upsert(namespace_for(space_id), vectors, metas)
        for i, row in enumerate(rows):
            row.vector = matrix[i].tobytes()
            row.vector_model = embedder.name
        self.session.flush()
        self.rebuild_lexical_index(space_id)
        return len(rows)

    def rebuild_lexical_index(self, space_id: int) -> int:
        docs = [
            (chunk.id, chunk.text, chunk.meta or {})
            for chunk in self.session.scalars(select(Chunk).where(Chunk.space_id == space_id)).all()
        ]
        size = index_registry.rebuild(namespace_for(space_id), docs)
        index_registry.touch_version(namespace_for(space_id))
        return size

    def ensure_lexical_index(self, space_id: int) -> int:
        ns = namespace_for(space_id)
        index = index_registry.get(ns)
        if index.size:
            return index.size
        return self.rebuild_lexical_index(space_id)

    def chunk_count(self, space_id: int | None = None) -> int:
        stmt = select(func.count()).select_from(Chunk)
        if space_id is not None:
            stmt = stmt.where(Chunk.space_id == (space_id or 0))
        return int(self.session.scalar(stmt) or 0)

    def list_chunks(self, doc_id: int) -> list[Chunk]:
        return list(
            self.session.scalars(select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.ordinal)).all()
        )

    def delete_document(self, doc_id: int) -> None:
        doc = self.session.get(DocumentRecord, doc_id)
        if doc is None:
            return
        space_id = doc.space_id or 0
        chunk_ids = [c.id for c in self.list_chunks(doc_id)]
        if chunk_ids:
            self.registry.vector_store().delete(namespace_for(space_id), chunk_ids)
        self.session.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
        if doc.storage_key:
            try:
                self.registry.object_store().delete(doc.storage_key)
            except Exception:  # noqa: BLE001
                pass
        self.session.delete(doc)
        self.session.flush()
        self.rebuild_lexical_index(space_id)

    def corpus_sample(self, space_id: int | None, limit: int = 400) -> list[str]:
        stmt = select(Chunk.text)
        if space_id is not None:
            stmt = stmt.where(Chunk.space_id == (space_id or 0))
        return list(self.session.scalars(stmt.limit(limit)).all())

    def preview_chunks(self, text: str, *, max_tokens: int | None = None) -> list[dict]:
        """切块预览：让前端在正式入库前看到分块效果。"""
        from ...providers.docparser.parsers import DefaultDocParser

        parsed = DefaultDocParser().parse(text.encode("utf-8"), "md", "preview.md")
        drafts = chunk_blocks(parsed.blocks, max_tokens=max_tokens or settings.chunk_max_tokens)
        return [
            {"ordinal": i, "locator": d.locator, "tokens": d.tokens, "text": truncate(d.text, 300)}
            for i, d in enumerate(drafts)
        ]


def _guess_doc_type(filename: str, title: str) -> str:
    clues = {
        "监管制度": ("办法", "规定", "指引", "通知", "巴塞尔", "ifrs", "cas", "标准", "规范"),
        "合同模板": ("合同", "协议", "条款", "模板"),
        "产品说明": ("产品", "说明书", "要素", "额度"),
        "业务手册": ("手册", "操作规程", "作业指导", "流程"),
        "报表数据": ("报表", "数据", "csv", "xlsx", "明细"),
        "会议纪要": ("纪要", "评审", "会议"),
    }
    blob = f"{filename} {title}".lower()
    for doc_type, words in clues.items():
        if any(w in blob for w in words):
            return doc_type
    return "参考资料"


__all__ = ["IngestService", "namespace_for", "estimate_tokens"]
