"""领域包安装：把 DomainPack 声明的基线本体与示例语料写入一个本体空间。

三步全部幂等——已存在的空间/类型/同名文档会被跳过或合并更新，因此「安装」可以反复点击，
也便于冒烟脚本在全新数据库上重放。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....models import DocumentRecord, RelationType, Space
from ...rag.ingest import IngestService
from ...wiki.compiler import WikiCompiler
from ..service import OntologyService
from .base import DomainPack


def get_or_create_space(session: Session, pack: DomainPack) -> Space:
    """按领域包定位空间；不存在则以 pack.key 作为 space.domain 创建。"""
    space = session.scalar(select(Space).where(Space.name == pack.space_name))
    if space is None:
        space = OntologyService(session).create_space(
            name=pack.space_name,
            domain=pack.key,
            description=pack.space_description,
            iri=pack.space_iri,
        )
        session.flush()
    elif space.domain != pack.key:
        space.domain = pack.key
    return space


def seed_space(session: Session, pack: DomainPack, space: Space) -> dict[str, Any]:
    """写入基线对象类型与关联断言；重复标签走 update 合并，不会产生副本。"""
    service = OntologyService(session)
    created = 0
    for payload in pack.object_types:
        if service.create_type(space.id, payload, origin="seed", status="active", confidence=1.0):
            created += 1

    for source_label, rel_label, target_label, local_name in pack.links:
        source = service.find_type_by_label(space.id, source_label)
        target = service.find_type_by_label(space.id, target_label)
        if not source or not target:
            continue
        if not session.scalar(
            select(RelationType).where(RelationType.space_id == space.id, RelationType.label == rel_label)
        ):
            session.add(
                RelationType(
                    space_id=space.id,
                    iri=f"{space.iri}prop/{local_name}",
                    label=rel_label,
                    label_en=local_name,
                    category=pack.category_of(local_name),
                    description=f"{source_label} 与 {target_label} 之间的「{rel_label}」语义",
                )
            )
            session.flush()
        try:
            service.link(space.id, source.id, target.id, rel_label, origin="seed", confidence=1.0)
        except ValueError:
            pass
    session.flush()
    return {"created": created, "types": len(service.list_types(space.id, limit=5000))}


def sample_manifest(pack: DomainPack, server_root: Path) -> list[dict[str, Any]]:
    """列出领域包随包发布的示例文档及其元数据。"""
    directory = pack.sample_dir(server_root)
    rows: list[dict[str, Any]] = []
    for sample in pack.samples:
        path = directory / sample.filename
        if not path.is_file():
            continue
        rows.append(
            {
                "pack": pack.key,
                "pack_label": pack.label,
                "filename": sample.filename,
                "doc_type": sample.doc_type,
                "issuer": sample.issuer,
                "jurisdiction": sample.jurisdiction,
                "authority": sample.authority,
                "size_bytes": path.stat().st_size,
            }
        )
    return rows


def ingest_pack_samples(session: Session, pack: DomainPack, space_id: int, server_root: Path) -> dict[str, Any]:
    """按清单入库示例文档；同名文档已存在则跳过。"""
    ingest = IngestService(session)
    existing = {row.name for row in session.scalars(select(DocumentRecord).where(DocumentRecord.space_id == space_id))}
    ingested, skipped = [], []
    for item in sample_manifest(pack, server_root):
        if item["filename"] in existing:
            skipped.append(item["filename"])
            continue
        report = ingest.ingest_file(
            pack.sample_dir(server_root) / item["filename"],
            space_id=space_id,
            doc_type=item["doc_type"],
            issuer=item["issuer"],
            jurisdiction=item["jurisdiction"],
            authority=item["authority"],
        )
        ingested.append({"filename": item["filename"], "doc_id": report.doc_id, "chunks": report.chunks})
    return {"ingested": ingested, "skipped": skipped, "total_chunks": ingest.chunk_count(space_id)}


def install_pack(
    session: Session,
    pack: DomainPack,
    server_root: Path,
    *,
    load_samples: bool = True,
    compile_wiki: bool = True,
    seed_ontology: bool = True,
) -> dict[str, Any]:
    """建空间 → 灌基线本体 → 示例语料入库 → 编译 Wiki，返回每一步的统计。"""
    service = OntologyService(session)
    space = get_or_create_space(session, pack)
    already = bool(service.list_types(space.id, limit=1))
    report: dict[str, Any] = {
        "pack": pack.key,
        "pack_label": pack.label,
        "space_id": space.id,
        "space_name": space.name,
    }
    if not seed_ontology:
        report["seed"] = {"skipped": "按请求跳过基线本体"}
    else:
        report["seed"] = {"skipped": "基线本体已存在"} if already else seed_space(session, pack, space)
    if load_samples:
        report["samples"] = ingest_pack_samples(session, pack, space.id, server_root)
    if compile_wiki:
        result = WikiCompiler(session).compile_space(space.id)
        report["wiki"] = {
            "created": result.created,
            "updated": result.updated,
            "unchanged": result.unchanged,
            "skipped_locked": result.skipped_locked,
        }
    report["stats"] = service.space_stats(space.id)
    return report
