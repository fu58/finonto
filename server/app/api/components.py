"""组件中心接口：目录、切换、探针、密钥状态、部署清单。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import COMPONENT_KEYS, get_settings
from ..db import get_db
from ..providers.registry import CATALOG, CATEGORY_LABELS, activate, catalog, probe

router = APIRouter(prefix="/api/components", tags=["components"])


@router.get("")
def list_components(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    return {
        "categories": catalog(settings),
        "selections": {key: getattr(settings, key) for key in COMPONENT_KEYS},
        "secrets": settings.secret_flags(),
        "components_file": str(settings.components_path),
        "env_prefix": "FINONTO_",
    }


@router.post("/activate")
def set_active(payload: dict = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    category = (payload.get("category") or "").strip()
    key = (payload.get("key") or "").strip()
    if not category or not key:
        raise HTTPException(status_code=400, detail="category 与 key 必填")
    try:
        return activate(category, key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/probe")
def run_probe(
    category: str = Query(...),
    key: str = Query(""),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return probe(category, key)


@router.get("/probe/all")
def probe_all(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    results = {}
    for component_key in COMPONENT_KEYS:
        selection = getattr(settings, component_key)
        results[component_key] = probe(component_key, selection)
    return {"probes": results, "selections": {key: getattr(settings, key) for key in COMPONENT_KEYS}}


@router.get("/secrets")
def secrets(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    return {"configured": settings.secret_flags(), "note": "只回显是否已配置，不返回密钥原文"}


@router.get("/deploy")
def deploy_manifest(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    return {
        "target": settings.deploy_target,
        "database_url": settings.resolved_database_url(),
        "data_dir": str(settings.data_path),
        "single_port": True,
        "targets": CATALOG["deploy_target"],
        "notes": [
            "单进程部署：FastAPI 同时托管模板与静态资源",
            "SQLite 单写者：多副本时用外部 Postgres，且副本数保持 1",
            "沙箱需要可执行子进程：受限运行时请设 FINONTO_SANDBOX_ENABLED=false",
        ],
    }


@router.get("/catalog/{category}")
def raw_catalog(category: str) -> dict[str, Any]:
    if category not in CATALOG:
        raise HTTPException(status_code=404, detail=f"未知类别：{category}")
    return {"category": category, "label": CATEGORY_LABELS.get(category, category), "entries": CATALOG[category]}
