"""系统接口：健康总览、数据规模计数、示例语料清单与一键冷启动。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import SERVER_DIR, get_settings
from ..core.ontology.packs import all_packs
from ..db import get_session
from ..providers.registry import registry
from ..services import bootstrap

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/info")
def info() -> dict[str, Any]:
    s = get_settings()
    return {
        "app_name": s.app_name,
        "offline_ready": not bool(s.llm_api_key),
        "data_dir": str(s.data_dir),
        "database_url": s.database_url or f"sqlite:///{s.data_dir / 'finonto.db'}",
        "active_components": {
            kind: registry.active_name(kind)
            for kind in ("llm", "embedding", "vector_store", "object_store", "web_search", "im_channel", "rerank")
        },
        "counts": {},
    }


@router.get("/status")
def status(session: Session = Depends(get_session)) -> dict[str, Any]:
    """前端顶栏的实时计数：本体类型 / 文档 / 片段 / 待确认提案 / Wiki 页 / Agent 运行 / 记忆条目。"""
    return {"counts": bootstrap.counters(session), "health": registry.health()}


@router.get("/samples")
def samples() -> list[dict[str, Any]]:
    """所有内置领域包示例语料的合并清单。"""
    return bootstrap.sample_manifest_all(SERVER_DIR)


@router.get("/packs")
def packs_available() -> list[dict[str, str]]:
    """已注册的领域包 key/label，供前端选择冷启动范围。"""
    return [{"key": pack.key, "label": pack.label, "summary": pack.summary} for pack in all_packs()]


@router.post("/bootstrap")
def run_bootstrap(
    pack_keys: str = "",
    load_samples: bool = True,
    seed_ontology: bool = True,
    compile_wiki: bool = True,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """幂等冷启动：领域包基线本体 → 示例文档入库 → 首次编译 Wiki。

    `pack_keys` 为逗号分隔的领域包 key；留空则装入全部内置领域包。
    """
    try:
        keys = [item.strip() for item in pack_keys.split(",") if item.strip()] or None
        bootstrap.pack_keys_for(keys)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    return bootstrap.bootstrap(
        session,
        SERVER_DIR,
        pack_keys=keys,
        seed_ontology=seed_ontology,
        load_samples=load_samples,
        compile_wiki=compile_wiki,
    )
