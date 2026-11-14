"""系统接口：健康检查、空间、领域包、冷启动。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.ontology import packs, service
from ..db import get_db
from ..providers.registry import get_components

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    components = get_components()
    spaces = service.list_spaces(db)
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": "0.1.0",
        "env": settings.env,
        "spaces": len(spaces),
        "components": {
            "llm": getattr(components.llm, "name", ""),
            "embedding": components.embedding.name,
            "vector_store": components.vector_store.name,
            "object_store": components.object_store.name,
            "rerank": components.reranker.name,
            "web_search": getattr(components.web_search, "name", ""),
            "im": getattr(components.im, "name", ""),
        },
        "sandbox_enabled": settings.sandbox_enabled,
        "data_dir": str(settings.data_path),
    }


@router.get("/spaces")
def list_spaces(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"spaces": service.list_spaces(db)}


@router.get("/spaces/{space_id}")
def space_detail(space_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    summary = service.space_summary(db, space_id)
    if summary.get("status") == "missing":
        raise HTTPException(status_code=404, detail=f"空间不存在：{space_id}")
    return summary


@router.get("/ontology/packs")
def packs_gallery(db: Session = Depends(get_db)) -> dict[str, Any]:
    installed = {item["key"]: item for item in service.list_spaces(db)}
    items = packs.gallery()
    for item in items:
        space = installed.get(item["key"])
        item["installed"] = space is not None
        item["space"] = space
    return {"packs": items, "count": len(items)}


@router.post("/ontology/packs/{pack_key}/install")
def install_pack(
    pack_key: str,
    ingest_samples: bool = Query(True),
    compile_wiki: bool = Query(True),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not packs.pack_exists(pack_key):
        raise HTTPException(status_code=404, detail=f"未知领域包：{pack_key}")
    settings = get_settings()
    result = service.install_pack(
        db,
        pack_key,
        components=get_components(),
        compile_wiki=compile_wiki,
        ingest_samples=ingest_samples,
        settings=settings,
    )
    return result


@router.get("/system/packs")
def installed_packs(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"packs": service.list_spaces(db)}


@router.get("/system/samples")
def samples(pack_key: str = Query(""), db: Session = Depends(get_db)) -> dict[str, Any]:
    if pack_key:
        return {"samples": packs.get_pack(pack_key).sample_manifest(), "pack": pack_key}
    return {
        "samples": [
            {"pack": item.key, "documents": item.sample_manifest()} for item in packs.all_packs()
        ]
    }


@router.post("/system/bootstrap")
def bootstrap(
    pack_keys: str = Query("", description="逗号分隔；留空表示全部内置领域包"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    keys = [key.strip() for key in pack_keys.split(",") if key.strip()] or [
        item.key for item in packs.all_packs()
    ]
    unknown = [key for key in keys if not packs.pack_exists(key)]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知领域包：{', '.join(unknown)}")
    settings = get_settings()
    results = [
        service.install_pack(db, key, components=get_components(), settings=settings)
        for key in keys
    ]
    return {
        "status": "bootstrapped",
        "packs": results,
        "spaces": service.list_spaces(db),
    }


@router.get("/ontology/{space_id}/vocab")
def vocab(space_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    summary = service.space_summary(db, space_id)
    if summary.get("status") == "missing":
        raise HTTPException(status_code=404, detail="空间不存在")
    return packs.vocab_for(summary["domain"])
