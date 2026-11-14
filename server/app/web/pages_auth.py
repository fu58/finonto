"""账号、审计与运维页面（与业务页面分开，便于按角色裁剪）。

这些页面都需要登录，其中 ``/users``、``/ops`` 仅管理员可见，``/audit`` 审批者以上可见。
权限在中间件里还有一道路径级校验（``middleware.WRITE_RULES``），这里再判一次，
是因为「页面渲染前的早退」比「渲染到一半再报错」体验好得多。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core import audit, auth_service, backup as backup_service, diagnostics
from ..db import get_db
from ..middleware import SESSION_COOKIE
from ..observability import METRICS
from ..security import (
    DEFAULT_ADMIN_PASSWORD,
    ROLE_DESCRIPTIONS,
    ROLE_LABELS,
    ROLE_LEVELS,
    client_ip,
    role_label,
    user_agent,
)
from .pages import deny, form_data, space_of
from .render import page_context, render

router = APIRouter(tags=["account"])


# --------------------------------------------------------------------------- #
# 登录 / 退出
# --------------------------------------------------------------------------- #
@router.get("/login")
def page_login(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    if not settings.auth_enabled:
        return RedirectResponse("/", status_code=303)
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse(request.query_params.get("next") or "/", status_code=303)

    hint = None
    try:
        users = auth_service.list_users(db)
        if len(users) == 1 and users[0]["must_change_password"]:
            hint = {
                "username": users[0]["username"],
                "password": settings.admin_password,
                "default_password": settings.admin_password == DEFAULT_ADMIN_PASSWORD,
            }
    except Exception:  # noqa: BLE001 - 登录页不能因为查询失败而 500
        hint = None

    return render(
        "login.html",
        {
            "username": "",
            "error": "会话已失效，请重新登录" if request.query_params.get("changed") else "",
            "next_url": request.query_params.get("next") or "/",
            "request_id": getattr(request.state, "request_id", ""),
            "bootstrap_hint": hint,
            "auth_enabled": settings.auth_enabled,
        },
        status_code=200,
    )


@router.post("/login")
async def do_login(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    data = await form_data(request)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    next_url = data.get("next") or "/"
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"

    user, reason = auth_service.authenticate(db, username, password)
    if user is None:
        audit.record(
            db,
            action="login_failed",
            target=username or "(空)",
            status="failed",
            detail={"reason": reason},
            actor=username or "anonymous",
            request=request,
        )
        db.commit()
        messages = {
            "empty": "请输入用户名与密码",
            "not_found": "用户名或密码不正确",
            "disabled": "账号已被停用，请联系管理员",
            "locked": "尝试次数过多，账号已临时锁定，请稍后再试",
            "bad_password": "用户名或密码不正确",
        }
        return render(
            "login.html",
            {
                "username": username,
                "error": messages.get(reason, "登录失败"),
                "next_url": next_url,
                "request_id": getattr(request.state, "request_id", ""),
                "bootstrap_hint": None,
                "auth_enabled": True,
            },
            status_code=401,
        )

    token, row = auth_service.create_session(
        db, user, ip=client_ip(request), user_agent=user_agent(request)
    )
    audit.record(
        db,
        action="login",
        target=user.username,
        detail={"role": user.role, "must_change_password": user.must_change_password},
        actor=user.username,
        role=user.role,
        request=request,
    )
    db.commit()

    response = RedirectResponse("/profile" if user.must_change_password else next_url, status_code=303)
    max_age = max(60, int((row.expires_at - row.created_at).total_seconds()))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return response


@router.get("/logout")
def do_logout(request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    auth_service.revoke_session(db, request.cookies.get(SESSION_COOKIE))
    audit.record(db, action="logout", target=getattr(user, "username", ""), request=request)
    db.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# --------------------------------------------------------------------------- #
# 账号设置（任意已登录用户）
# --------------------------------------------------------------------------- #
@router.get("/profile")
def page_profile(request: Request, db: Session = Depends(get_db)):
    denied = deny(request, "viewer")
    if denied:
        return denied
    user = getattr(request.state, "user", None)
    context = page_context(
        db, request=request, space_id=space_of(request), active="profile", current_path="/profile"
    )
    context.update(
        {
            "profile_user": user,
            "sessions": auth_service.session_stats(db),
            "message": "",
            "message_kind": "",
        }
    )
    return render("profile.html", context)


@router.post("/profile/password")
async def change_password_page(request: Request, db: Session = Depends(get_db)):
    denied = deny(request, "viewer")
    if denied:
        return denied
    data = await form_data(request)
    user = getattr(request.state, "user", None)
    result = auth_service.change_password(
        db, user, data.get("old_password", ""), data.get("new_password", "")
    )
    if result.get("status") != "changed":
        context = page_context(
            db, request=request, space_id=space_of(request), active="profile", current_path="/profile"
        )
        context.update(
            {
                "profile_user": user,
                "sessions": auth_service.session_stats(db),
                "message": result.get("message", "修改失败"),
                "message_kind": "err",
            }
        )
        return render("profile.html", context, status_code=400)

    audit.record(db, action="change_password", target=user.username, request=request)
    db.commit()
    response = RedirectResponse("/login?changed=1", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# --------------------------------------------------------------------------- #
# 用户与角色（admin）
# --------------------------------------------------------------------------- #
@router.get("/users")
def page_users(request: Request, db: Session = Depends(get_db)):
    denied = deny(request, "admin")
    if denied:
        return denied
    context = page_context(
        db, request=request, space_id=space_of(request), active="users", current_path="/users"
    )
    context.update(
        {
            "users": auth_service.list_users(db),
            "session_stats": auth_service.session_stats(db),
            "roles": [
                {"key": key, "label": ROLE_LABELS[key], "level": level,
                 "description": ROLE_DESCRIPTIONS[key]}
                for key, level in ROLE_LEVELS.items()
            ],
            "message": request.query_params.get("message", ""),
        }
    )
    return render("users.html", context)


@router.post("/users/create")
async def create_user_page(request: Request, db: Session = Depends(get_db)):
    denied = deny(request, "admin")
    if denied:
        return denied
    data = await form_data(request)
    result = auth_service.create_user(
        db,
        data.get("username", ""),
        data.get("password", ""),
        role=data.get("role", "viewer"),
        display_name=data.get("display_name", ""),
    )
    audit.record(
        db,
        action="create_user",
        target=result.get("username") or data.get("username", ""),
        status="ok" if result.get("status") == "created" else "failed",
        detail={"result": result.get("status"), "role": data.get("role")},
        request=request,
    )
    db.commit()
    return RedirectResponse(f"/users?message={result.get('status')}", status_code=303)


@router.post("/users/{user_id}/update")
async def update_user_page(user_id: int, request: Request, db: Session = Depends(get_db)):
    denied = deny(request, "admin")
    if denied:
        return denied
    data = await form_data(request)
    actor = getattr(request.state, "user", None)
    active_raw = data.get("is_active", "")
    is_active = None if active_raw == "" else active_raw.lower() in ("1", "true", "on")
    result = auth_service.update_user(
        db,
        user_id,
        role=data.get("role") or None,
        is_active=is_active,
        display_name=data.get("display_name"),
        actor_id=getattr(actor, "id", None),
    )
    audit.record(
        db,
        action="update_user",
        target=str(user_id),
        status="ok" if result.get("status") == "updated" else "denied",
        detail={"role": data.get("role"), "is_active": is_active, "result": result.get("status")},
        request=request,
    )
    db.commit()
    return RedirectResponse(f"/users?message={result.get('status')}", status_code=303)


@router.post("/users/{user_id}/reset")
async def reset_password_page(user_id: int, request: Request, db: Session = Depends(get_db)):
    denied = deny(request, "admin")
    if denied:
        return denied
    data = await form_data(request)
    result = auth_service.reset_password(db, user_id, data.get("password", ""))
    audit.record(
        db,
        action="reset_password",
        target=str(user_id),
        status="ok" if result.get("status") == "reset" else "failed",
        request=request,
    )
    db.commit()
    return RedirectResponse(f"/users?message={result.get('status')}", status_code=303)


@router.post("/users/{user_id}/delete")
def delete_user_page(user_id: int, request: Request, db: Session = Depends(get_db)):
    denied = deny(request, "admin")
    if denied:
        return denied
    actor = getattr(request.state, "user", None)
    result = auth_service.delete_user(db, user_id, actor_id=getattr(actor, "id", None))
    audit.record(
        db,
        action="delete_user",
        target=str(user_id),
        status="ok" if result.get("status") == "deleted" else "denied",
        request=request,
    )
    db.commit()
    return RedirectResponse(f"/users?message={result.get('status')}", status_code=303)


# --------------------------------------------------------------------------- #
# 审计日志（approver 以上）
# --------------------------------------------------------------------------- #
@router.get("/audit")
def page_audit(request: Request, db: Session = Depends(get_db)):
    denied = deny(request, "approver")
    if denied:
        return denied
    filters = {
        "action": request.query_params.get("action", ""),
        "actor": request.query_params.get("actor", ""),
        "status": request.query_params.get("status", ""),
        "keyword": request.query_params.get("keyword", ""),
    }
    context = page_context(
        db, request=request, space_id=space_of(request), active="audit", current_path="/audit"
    )
    context.update(
        {
            "logs": audit.list_logs(db, limit=200, **filters),
            "stats": audit.stats(db),
            "actors": audit.distinct_actors(db),
            "actions": [{"key": key, "label": audit.action_label(key)} for key in sorted(audit.ACTIONS)],
            "filters": filters,
        }
    )
    return render("audit.html", context)


# --------------------------------------------------------------------------- #
# 运维（admin）：诊断 / 备份 / 指标
# --------------------------------------------------------------------------- #
@router.get("/ops")
def page_ops(request: Request, db: Session = Depends(get_db)):
    denied = deny(request, "admin")
    if denied:
        return denied
    context = page_context(
        db, request=request, space_id=space_of(request), active="ops", current_path="/ops"
    )
    context.update(
        {
            "diagnostics": diagnostics.run_checks(),
            "backups": backup_service.list_backups(),
            "metrics": METRICS.snapshot(),
            "sessions": auth_service.session_stats(db),
            "restore_hint": backup_service.restore_hint(),
            "settings": get_settings(),
            "message": request.query_params.get("message", ""),
        }
    )
    return render("ops.html", context)


@router.post("/ops/backup")
async def backup_page(request: Request, db: Session = Depends(get_db)):
    denied = deny(request, "admin")
    if denied:
        return denied
    data = await form_data(request)
    result = backup_service.create_backup(note=data.get("note", ""))
    audit.record(
        db, action="backup", target=result["name"], detail={"bytes": result["bytes"]}, request=request
    )
    db.commit()
    return RedirectResponse(f"/ops?message=backup:{result['name']}", status_code=303)


@router.post("/ops/backup/{name}/delete")
def delete_backup_page(name: str, request: Request, db: Session = Depends(get_db)):
    denied = deny(request, "admin")
    if denied:
        return denied
    result = backup_service.delete_backup(name)
    audit.record(db, action="backup", target=f"delete:{name}", request=request)
    db.commit()
    return RedirectResponse(f"/ops?message={result.get('status')}", status_code=303)


@router.post("/ops/audit-purge")
async def purge_audit_page(request: Request, db: Session = Depends(get_db)):
    denied = deny(request, "admin")
    if denied:
        return denied
    data = await form_data(request)
    days = int(data.get("days") or get_settings().audit_retention_days)
    removed = audit.purge(db, days=days)
    audit.record(db, action="purge_audit", target=f"{days}d", detail={"removed": removed}, request=request)
    db.commit()
    return RedirectResponse(f"/ops?message=purged:{removed}", status_code=303)
