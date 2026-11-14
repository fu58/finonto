"""运维接口：指标、就绪探针、诊断、备份、审计查询与导出。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core import audit, auth_service, backup, diagnostics
from ..db import get_db
from ..middleware import require_role
from ..models import AuditLog, ChangeProposal, Document, ObjectType, WikiPage
from ..observability import METRICS, prometheus_text

router = APIRouter(prefix="/api", tags=["ops"])


# --------------------------------------------------------------------------- #
# 探针与指标（公开，供负载均衡 / Prometheus 抓取；不含敏感数据）
# --------------------------------------------------------------------------- #
@router.get("/ready")
def ready() -> dict[str, Any]:
    result = diagnostics.readiness()
    if not result["ready"]:
        return {"status": "not_ready", **result}
    return {"status": "ready", **result}


@router.get("/metrics")
def metrics(db: Session = Depends(get_db)) -> PlainTextResponse:
    gauges: dict[str, float] = {}
    try:
        gauges = {
            "documents_total": float(db.execute(select(func.count(Document.id))).scalar_one()),
            "object_types_total": float(db.execute(select(func.count(ObjectType.id))).scalar_one()),
            "wiki_pages_total": float(db.execute(select(func.count(WikiPage.id))).scalar_one()),
            "pending_proposals": float(
                db.execute(
                    select(func.count(ChangeProposal.id)).where(ChangeProposal.status == "pending")
                ).scalar_one()
            ),
            "audit_records_total": float(db.execute(select(func.count(AuditLog.id))).scalar_one()),
        }
    except Exception:  # noqa: BLE001 - 指标端点不能因为查询失败而 500
        gauges = {}
    return PlainTextResponse(prometheus_text(gauges), media_type="text/plain; version=0.0.4; charset=utf-8")


# --------------------------------------------------------------------------- #
# 诊断（admin）
# --------------------------------------------------------------------------- #
@router.get("/system/diagnostics")
def system_diagnostics(request: Request, db: Session = Depends(get_db), user=Depends(require_role("admin"))) -> dict[str, Any]:
    settings = get_settings()
    result = diagnostics.run_checks()
    return {
        **result,
        "runtime": {
            "auth_enabled": settings.auth_enabled,
            "ldap": "未集成（账号为内置）",
            "llm": settings.llm_provider,
            "embedding": settings.embedding_provider,
            "vector_store": settings.vector_store,
            "data_dir": str(settings.data_path),
            "database_url": settings.resolved_database_url(),
            "deploy_target": settings.deploy_target,
            "sessions": auth_service.session_stats(db),
            "metrics": METRICS.snapshot(),
            "rate_limit": {"per_minute": settings.rate_limit_per_min},
        },
    }


# --------------------------------------------------------------------------- #
# 备份（admin）
# --------------------------------------------------------------------------- #
@router.get("/system/backups")
def list_backups(db: Session = Depends(get_db), user=Depends(require_role("admin"))) -> dict[str, Any]:
    return {"backups": backup.list_backups(), "restore_hint": backup.restore_hint()}


@router.post("/system/backup")
def create_backup(
    request: Request,
    payload: dict = Body(default={}),
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
) -> dict[str, Any]:
    note = (payload.get("note") or "").strip()
    result = backup.create_backup(note=note)
    audit.record(
        db, action="backup", target=result["name"], detail={"bytes": result["bytes"], "note": note}, request=request
    )
    db.commit()
    return result


@router.delete("/system/backups/{name}")
def delete_backup(name: str, request: Request, db: Session = Depends(get_db), user=Depends(require_role("admin"))) -> dict[str, Any]:
    result = backup.delete_backup(name)
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail="备份不存在")
    audit.record(db, action="backup", target=f"delete:{result['name']}", request=request)
    db.commit()
    return result


# --------------------------------------------------------------------------- #
# 审计（approver 以上）
# --------------------------------------------------------------------------- #
@router.get("/audit")
def list_audit(
    action: str = Query(""),
    actor: str = Query(""),
    status: str = Query(""),
    keyword: str = Query(""),
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
    user=Depends(require_role("approver")),
) -> dict[str, Any]:
    return {
        "logs": audit.list_logs(db, action=action, actor=actor, status=status, keyword=keyword, limit=limit),
        "stats": audit.stats(db),
        "actors": audit.distinct_actors(db),
        "actions": [
            {"key": key, "label": audit.action_label(key)} for key in sorted(audit.ACTIONS)
        ],
    }


@router.get("/audit/export.csv")
def export_audit(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_role("approver")),
) -> PlainTextResponse:
    audit.record(db, action="purge_audit", target="export_csv", request=request)
    db.commit()
    content = audit.export_csv(db)
    return PlainTextResponse(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=finonto-audit.csv"},
    )


@router.post("/audit/purge")
def purge_audit(
    request: Request,
    payload: dict = Body(default={}),
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
) -> dict[str, Any]:
    settings = get_settings()
    days = int(payload.get("days") or settings.audit_retention_days)
    removed = audit.purge(db, days=days)
    audit.record(db, action="purge_audit", target=f"{days}d", detail={"removed": removed}, request=request)
    db.commit()
    return {"status": "purged", "removed": removed, "keep_days": days}
