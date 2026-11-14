"""认证与用户管理接口（JSON）。页面表单路径见 web/pages.py。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core import audit
from ..core import auth_service
from ..db import get_db
from ..middleware import SESSION_COOKIE, require_role
from ..security import (
    ROLE_DESCRIPTIONS,
    ROLE_LABELS,
    ROLE_LEVELS,
    client_ip,
    password_problem,
    user_agent,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def set_session_cookie(response: Response, token: str, *, max_age: int) -> None:
    settings = get_settings()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def user_public(user) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "role_label": ROLE_LABELS.get(user.role, user.role),
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        "last_login_at": user.last_login_at.isoformat(sep=" ", timespec="seconds") if user.last_login_at else "",
    }


@router.post("/login")
def login(
    request: Request,
    response: Response,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    user, reason = auth_service.authenticate(db, username, password)
    if user is None:
        audit.record(
            db,
            action="login_failed",
            target=username or "(空用户名)",
            status="failed",
            detail={"reason": reason},
            actor=username or "anonymous",
            request=request,
        )
        db.commit()
        messages = {
            "empty": "请输入用户名与密码",
            "not_found": "用户名或密码不正确",
            "disabled": "账号已被停用",
            "locked": "尝试次数过多，账号已临时锁定",
            "bad_password": "用户名或密码不正确",
        }
        raise HTTPException(status_code=401, detail=messages.get(reason, "登录失败"))

    token, row = auth_service.create_session(
        db, user, ip=client_ip(request), user_agent=user_agent(request)
    )
    set_session_cookie(response, token, max_age=int((row.expires_at - row.created_at).total_seconds()))
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
    return {"status": "ok", "user": user_public(user), "token_ttl_hours": get_settings().session_ttl_hours}


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
    token = request.cookies.get(SESSION_COOKIE)
    user = getattr(request.state, "user", None)
    auth_service.revoke_session(db, token)
    clear_session_cookie(response)
    audit.record(
        db, action="logout", target=getattr(user, "username", ""), status="ok", request=request
    )
    db.commit()
    return {"status": "logged_out"}


@router.get("/me")
def me(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if user is None:
        return {"authenticated": False, "auth_enabled": get_settings().auth_enabled}
    return {"authenticated": True, "user": user_public(user), "auth_enabled": get_settings().auth_enabled}


@router.get("/roles")
def roles() -> dict[str, Any]:
    return {
        "roles": [
            {"key": key, "label": ROLE_LABELS[key], "level": level,
             "description": ROLE_DESCRIPTIONS[key]}
            for key, level in ROLE_LEVELS.items()
        ]
    }


@router.post("/change-password")
def change_password(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user=Depends(require_role("viewer")),
) -> dict[str, Any]:
    old = payload.get("old_password") or ""
    new = payload.get("new_password") or ""
    result = auth_service.change_password(db, user, old, new)
    if result.get("status") != "changed":
        raise HTTPException(status_code=400, detail=result.get("message", "修改失败"))
    audit.record(db, action="change_password", target=user.username, request=request)
    db.commit()
    return {"status": "changed", "relogin_required": True}


# --------------------------------------------------------------------------- #
# 用户管理（admin）
# --------------------------------------------------------------------------- #
@router.get("/users")
def list_users(db: Session = Depends(get_db), user=Depends(require_role("admin"))) -> dict[str, Any]:
    return {"users": auth_service.list_users(db), "session_stats": auth_service.session_stats(db)}


@router.post("/users")
def create_user(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
) -> dict[str, Any]:
    result = auth_service.create_user(
        db,
        payload.get("username", ""),
        payload.get("password", ""),
        role=payload.get("role", "viewer"),
        display_name=payload.get("display_name", ""),
    )
    if result.get("status") == "invalid":
        raise HTTPException(status_code=400, detail=result.get("message"))
    if result.get("status") == "conflict":
        raise HTTPException(status_code=409, detail=result.get("message"))
    audit.record(
        db, action="create_user", target=result.get("username", ""),
        detail={"role": result.get("role")}, request=request,
    )
    db.commit()
    return result


@router.patch("/users/{user_id}")
def update_user(
    user_id: int,
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
) -> dict[str, Any]:
    if user_id == user.id and payload.get("role") and payload["role"] != user.role:
        raise HTTPException(status_code=400, detail="不能修改自己的角色")
    result = auth_service.update_user(
        db,
        user_id,
        role=payload.get("role"),
        is_active=payload.get("is_active"),
        display_name=payload.get("display_name"),
        actor_id=user.id,
    )
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail="用户不存在")
    if result.get("status") == "conflict":
        raise HTTPException(status_code=409, detail=result.get("message"))
    audit.record(db, action="update_user", target=str(user_id), detail=payload, request=request)
    db.commit()
    return result


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
) -> dict[str, Any]:
    new_password = payload.get("password") or ""
    problem = password_problem(new_password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    result = auth_service.reset_password(db, user_id, new_password)
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail="用户不存在")
    audit.record(db, action="reset_password", target=str(user_id), request=request)
    db.commit()
    return result


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
) -> dict[str, Any]:
    result = auth_service.delete_user(db, user_id, actor_id=user.id)
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail="用户不存在")
    if result.get("status") == "conflict":
        raise HTTPException(status_code=409, detail=result.get("message"))
    audit.record(db, action="delete_user", target=str(user_id), request=request)
    db.commit()
    return result
