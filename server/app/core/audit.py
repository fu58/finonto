"""审计日志：记录「谁、什么时候、对什么、做了什么、结果如何」。

三条原则
--------
1. **审计写入永不阻断业务**：``record()`` 内部吞掉所有异常（只留一条 warning 日志），
   因为「审计失败导致提案确认报错」在事故复盘时是最糟的形态。
2. **不记录密钥与正文**：只记动作、目标标识与必要的结构化摘要（长度截断）。
3. **可导出**：合规检查通常要一份 CSV，所以 ``export_csv`` 与页面共用同一套查询。
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import Select, func, select

from ..db import session_scope
from ..models import AuditLog, utcnow

logger = logging.getLogger("finonto.audit")

#: 允许出现的动作（新增动作时同步这里的白名单，便于统计与前端筛选）
ACTIONS = {
    "login", "logout", "login_failed", "change_password",
    "create_user", "update_user", "delete_user", "reset_password",
    "accept_proposal", "reject_proposal",
    "create_type", "update_type", "delete_type",
    "ingest_document", "delete_document", "reindex",
    "edit_wiki", "lock_wiki", "rollback_wiki", "compile_wiki",
    "activate_component", "install_pack",
    "backup", "purge_audit", "export_ontology", "sandbox_run",
}

ACTION_LABELS: dict[str, str] = {
    "login": "登录", "logout": "退出", "login_failed": "登录失败",
    "change_password": "修改密码",
    "create_user": "创建用户", "update_user": "修改用户",
    "delete_user": "删除用户", "reset_password": "重置密码",
    "accept_proposal": "确认提案", "reject_proposal": "驳回提案",
    "create_type": "新建类型", "update_type": "修改类型", "delete_type": "删除类型",
    "ingest_document": "文档入库", "delete_document": "删除文档", "reindex": "重建索引",
    "edit_wiki": "编辑 Wiki", "lock_wiki": "锁定/解锁页面",
    "rollback_wiki": "回滚页面", "compile_wiki": "编译 Wiki",
    "activate_component": "切换组件", "install_pack": "安装领域包",
    "backup": "数据备份", "purge_audit": "清理审计",
    "export_ontology": "导出本体", "sandbox_run": "沙箱执行",
}

STATUS_LABELS = {"ok": "成功", "denied": "被拒绝", "failed": "失败"}


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action)


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def record(
    session=None,
    *,
    action: str,
    target: str = "",
    status: str = "ok",
    detail: dict[str, Any] | None = None,
    actor: str = "system",
    role: str = "",
    request=None,
    request_id: str = "",
) -> AuditLog | None:
    """写一条审计。``session`` 为空时自己开一个独立事务。

    绝不抛错：审计是旁路，不能把主流程拖挂。
    """
    try:
        payload = _sanitize(detail or {})
        ip = ""
        agent = ""
        rid = request_id
        if request is not None:
            from ..security import client_ip, user_agent

            ip = client_ip(request)
            agent = user_agent(request)
            state = getattr(request, "state", None)
            rid = rid or getattr(state, "request_id", "")
            if not actor or actor == "system":
                user = getattr(state, "user", None)
                if user is not None:
                    actor = user.username
                    role = role or user.role

        if session is None:
            try:
                with session_scope() as own:
                    row = AuditLog(
                        action=action, target=(target or "")[:200], status=status,
                        detail=payload, actor=(actor or "system")[:60], role=(role or "")[:20],
                        ip=ip, user_agent=agent, request_id=(rid or "")[:40],
                    )
                    own.add(row)
                    own.flush()
                    own.expunge(row)
                return row
            except Exception as exc:  # noqa: BLE001
                logger.warning("审计写入失败（已忽略）：%s", exc)
                return None

        row = AuditLog(
            action=action, target=(target or "")[:200], status=status,
            detail=payload, actor=(actor or "system")[:60], role=(role or "")[:20],
            ip=ip, user_agent=agent, request_id=(rid or "")[:40],
        )
        session.add(row)
        session.flush()
        return row
    except Exception as exc:  # noqa: BLE001
        logger.warning("审计写入失败（已忽略）：%s", exc)
        return None


def _sanitize(detail: dict[str, Any]) -> dict[str, Any]:
    """截断长文本、剔除明显的密钥字段。"""
    cleaned: dict[str, Any] = {}
    for key, value in (detail or {}).items():
        lowered = str(key).lower()
        if any(bad in lowered for bad in ("password", "secret", "token", "api_key", "webhook")):
            cleaned[key] = "***"
            continue
        if isinstance(value, str):
            cleaned[key] = value[:400]
        elif isinstance(value, (int, float, bool)) or value is None:
            cleaned[key] = value
        else:
            text = json.dumps(value, ensure_ascii=False, default=str)
            cleaned[key] = text[:400]
    return cleaned


# --------------------------------------------------------------------------- #
# 查询 / 导出 / 清理
# --------------------------------------------------------------------------- #
def _base_query(
    *,
    action: str = "",
    actor: str = "",
    status: str = "",
    keyword: str = "",
) -> Select:
    query = select(AuditLog)
    if action:
        query = query.where(AuditLog.action == action)
    if actor:
        query = query.where(AuditLog.actor == actor)
    if status:
        query = query.where(AuditLog.status == status)
    if keyword:
        like = f"%{keyword.strip()}%"
        query = query.where(AuditLog.target.like(like) | AuditLog.action.like(like))
    return query.order_by(AuditLog.id.desc())


def list_logs(
    session,
    *,
    action: str = "",
    actor: str = "",
    status: str = "",
    keyword: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    rows = session.execute(
        _base_query(action=action, actor=actor, status=status, keyword=keyword).limit(limit)
    ).scalars().all()
    return [
        {
            "id": row.id,
            "created_at": row.created_at.isoformat(sep=" ", timespec="seconds"),
            "actor": row.actor,
            "role": row.role,
            "action": row.action,
            "action_label": action_label(row.action),
            "target": row.target,
            "status": row.status,
            "status_label": status_label(row.status),
            "detail": row.detail or {},
            "ip": row.ip,
            "request_id": row.request_id,
        }
        for row in rows
    ]


def stats(session, *, hours: int = 24) -> dict[str, Any]:
    total = int(session.execute(select(func.count(AuditLog.id))).scalar_one())
    since = utcnow() - timedelta(hours=hours)
    recent = int(
        session.execute(
            select(func.count(AuditLog.id)).where(AuditLog.created_at >= since)
        ).scalar_one()
    )
    failures = int(
        session.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.status.in_(("failed", "denied")), AuditLog.created_at >= since
            )
        ).scalar_one()
    )
    by_action = session.execute(
        select(AuditLog.action, func.count(AuditLog.id))
        .where(AuditLog.created_at >= since)
        .group_by(AuditLog.action)
        .order_by(func.count(AuditLog.id).desc())
        .limit(10)
    ).all()
    return {
        "total": total,
        "recent": recent,
        "failures": failures,
        "window_hours": hours,
        "top_actions": [
            {"action": action, "label": action_label(action), "count": int(count)}
            for action, count in by_action
        ],
    }


def distinct_actors(session) -> list[str]:
    rows = session.execute(
        select(AuditLog.actor).group_by(AuditLog.actor).order_by(AuditLog.actor).limit(100)
    ).all()
    return [row[0] for row in rows if row[0]]


def export_csv(session, *, limit: int = 5000) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["时间", "操作人", "角色", "动作", "对象", "结果", "IP", "request_id", "详情"])
    rows = session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).scalars().all()
    for row in rows:
        writer.writerow(
            [
                row.created_at.isoformat(sep=" ", timespec="seconds"),
                row.actor,
                row.role,
                action_label(row.action),
                row.target,
                status_label(row.status),
                row.ip,
                row.request_id,
                json.dumps(row.detail or {}, ensure_ascii=False)[:500],
            ]
        )
    return buffer.getvalue()


def purge(session, *, days: int = 180) -> int:
    cutoff = utcnow() - timedelta(days=max(1, days))
    rows = session.execute(
        select(AuditLog.id).where(AuditLog.created_at < cutoff)
    ).all()
    if not rows:
        return 0
    ids = [row[0] for row in rows]
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        session.query(AuditLog).filter(AuditLog.id.in_(chunk)).delete(synchronize_session=False)
    return len(ids)
