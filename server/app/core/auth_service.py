"""账号与会话服务：初始化管理员、登录、会话解析、用户 CRUD。

会话安全要点
------------
* 令牌只存 SHA-256 指纹，库泄露不等于会话泄露；
* 过期、吊销、用户停用三条都要查（任何一条不满足即失效）；
* 登录失败计数 + 临时锁定，防在线爆破；
* 改密/停用/删除用户时**吊销其全部会话**，避免「改了密码旧 cookie 还能用」。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from ..config import Settings, get_settings
from ..models import SessionToken, User, utcnow
from ..security import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
    hash_password,
    new_session_token,
    password_problem,
    role_level,
    token_fingerprint,
    verify_password,
)


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #
def ensure_admin(session, settings: Settings | None = None) -> dict[str, Any]:
    """没有任何用户时创建管理员（幂等）。"""
    settings = settings or get_settings()
    count = int(session.execute(select(func.count(User.id))).scalar_one())
    if count > 0:
        return {"status": "exists", "users": count}

    username = (settings.admin_username or DEFAULT_ADMIN_USERNAME).strip() or DEFAULT_ADMIN_USERNAME
    password = settings.admin_password or DEFAULT_ADMIN_PASSWORD
    user = User(
        username=username,
        display_name="系统管理员",
        password_hash=hash_password(password),
        role="admin",
        is_active=True,
        must_change_password=True,
    )
    session.add(user)
    session.commit()
    return {
        "status": "created",
        "username": username,
        "role": "admin",
        "must_change_password": True,
        "default_password": password == DEFAULT_ADMIN_PASSWORD,
    }


# --------------------------------------------------------------------------- #
# 登录 / 会话
# --------------------------------------------------------------------------- #
def authenticate(
    session, username: str, password: str, settings: Settings | None = None
) -> tuple[User | None, str]:
    """返回 (用户, 失败原因)。原因值：empty / not_found / disabled / locked / bad_password。"""
    settings = settings or get_settings()
    username = (username or "").strip()
    if not username or not password:
        return None, "empty"

    user = session.execute(select(User).where(User.username == username)).scalars().first()
    if user is None:
        return None, "not_found"
    if not user.is_active:
        return None, "disabled"

    now = utcnow()
    if user.locked_until and user.locked_until > now:
        return None, "locked"

    if not verify_password(password, user.password_hash):
        user.failed_attempts = int(user.failed_attempts or 0) + 1
        if user.failed_attempts >= max(1, settings.auth_login_max_attempts):
            user.locked_until = now + timedelta(minutes=max(1, settings.auth_lock_minutes))
            user.failed_attempts = 0
        session.commit()
        return None, "bad_password"

    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    session.commit()
    return user, ""


def create_session(
    session,
    user: User,
    *,
    ip: str = "",
    user_agent: str = "",
    ttl_hours: int | None = None,
    settings: Settings | None = None,
) -> tuple[str, SessionToken]:
    settings = settings or get_settings()
    hours = int(ttl_hours or settings.session_ttl_hours or 12)
    token = new_session_token()
    row = SessionToken(
        token_hash=token_fingerprint(token),
        user_id=user.id,
        expires_at=utcnow() + timedelta(hours=max(1, hours)),
        ip=(ip or "")[:60],
        user_agent=(user_agent or "")[:200],
        last_seen_at=utcnow(),
    )
    session.add(row)
    session.commit()
    return token, row


def resolve_session(session, token: str | None) -> User | None:
    if not token:
        return None
    row = session.execute(
        select(SessionToken).where(SessionToken.token_hash == token_fingerprint(token))
    ).scalars().first()
    if row is None or row.revoked:
        return None
    now = utcnow()
    if row.expires_at <= now:
        return None
    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    row.last_seen_at = now
    session.commit()
    return user


def revoke_session(session, token: str | None) -> bool:
    if not token:
        return False
    row = session.execute(
        select(SessionToken).where(SessionToken.token_hash == token_fingerprint(token))
    ).scalars().first()
    if row is None:
        return False
    row.revoked = True
    session.commit()
    return True


def revoke_user_sessions(session, user_id: int) -> int:
    rows = session.execute(
        select(SessionToken).where(SessionToken.user_id == user_id, SessionToken.revoked.is_(False))
    ).scalars().all()
    for row in rows:
        row.revoked = True
    session.commit()
    return len(rows)


def cleanup_expired_sessions(session) -> int:
    cutoff = utcnow()
    rows = session.execute(select(SessionToken).where(SessionToken.expires_at < cutoff)).scalars().all()
    for row in rows:
        session.delete(row)
    session.commit()
    return len(rows)


def session_stats(session) -> dict[str, Any]:
    now = utcnow()
    total = int(session.execute(select(func.count(SessionToken.id))).scalar_one())
    active = int(
        session.execute(
            select(func.count(SessionToken.id)).where(
                SessionToken.revoked.is_(False), SessionToken.expires_at > now
            )
        ).scalar_one()
    )
    return {"total": total, "active": active}


# --------------------------------------------------------------------------- #
# 用户管理
# --------------------------------------------------------------------------- #
def list_users(session) -> list[dict[str, Any]]:
    rows = session.execute(select(User).order_by(User.id)).scalars().all()
    return [
        {
            "id": row.id,
            "username": row.username,
            "display_name": row.display_name,
            "role": row.role,
            "is_active": row.is_active,
            "must_change_password": row.must_change_password,
            "failed_attempts": row.failed_attempts,
            "locked_until": row.locked_until.isoformat(sep=" ", timespec="seconds") if row.locked_until else "",
            "last_login_at": row.last_login_at.isoformat(sep=" ", timespec="seconds") if row.last_login_at else "",
            "created_at": row.created_at.isoformat(sep=" ", timespec="seconds"),
        }
        for row in rows
    ]


def create_user(
    session,
    username: str,
    password: str,
    *,
    role: str = "viewer",
    display_name: str = "",
    must_change_password: bool = True,
) -> dict[str, Any]:
    username = (username or "").strip()
    if not username:
        return {"status": "invalid", "message": "用户名不能为空"}
    if role_level(role) <= 0:
        return {"status": "invalid", "message": f"未知角色：{role}"}
    problem = password_problem(password)
    if problem:
        return {"status": "invalid", "message": problem}
    exists = session.execute(select(User).where(User.username == username)).scalars().first()
    if exists is not None:
        return {"status": "conflict", "message": f"用户名已存在：{username}"}
    user = User(
        username=username,
        display_name=(display_name or "").strip(),
        password_hash=hash_password(password),
        role=role,
        is_active=True,
        must_change_password=bool(must_change_password),
    )
    session.add(user)
    session.commit()
    return {"status": "created", "id": user.id, "username": user.username, "role": user.role}


def update_user(
    session,
    user_id: int,
    *,
    role: str | None = None,
    is_active: bool | None = None,
    display_name: str | None = None,
    actor_id: int | None = None,
) -> dict[str, Any]:
    user = session.get(User, user_id)
    if user is None:
        return {"status": "missing"}
    if role is not None:
        if role_level(role) <= 0:
            return {"status": "invalid", "message": f"未知角色：{role}"}
        # 不允许把最后一个管理员降级，否则系统会锁死
        if user.role == "admin" and role != "admin" and _count_admins(session) <= 1:
            return {"status": "conflict", "message": "至少保留一个管理员"}
        user.role = role
    if display_name is not None:
        user.display_name = display_name.strip()
    if is_active is not None:
        if not is_active and user.role == "admin" and _count_admins(session) <= 1:
            return {"status": "conflict", "message": "至少保留一个启用的管理员"}
        if not is_active and actor_id == user.id:
            return {"status": "conflict", "message": "不能停用自己的账号"}
        user.is_active = bool(is_active)
        if not user.is_active:
            revoke_user_sessions(session, user.id)
    session.commit()
    return {"status": "updated", "id": user.id}


def reset_password(session, user_id: int, new_password: str, *, must_change: bool = True) -> dict[str, Any]:
    user = session.get(User, user_id)
    if user is None:
        return {"status": "missing"}
    problem = password_problem(new_password)
    if problem:
        return {"status": "invalid", "message": problem}
    user.password_hash = hash_password(new_password)
    user.must_change_password = bool(must_change)
    user.failed_attempts = 0
    user.locked_until = None
    session.commit()
    revoked = revoke_user_sessions(session, user.id)
    return {"status": "reset", "id": user.id, "revoked_sessions": revoked}


def change_password(
    session, user: User, old_password: str, new_password: str
) -> dict[str, Any]:
    # 注意：``user`` 可能来自 request.state（中间件用独立 session 加载后 expunge 的游离对象），
    # 对游离对象赋值不会被任何 session 跟踪。必须按 id 重新取一次再改。
    target = session.get(User, getattr(user, "id", 0)) if user is not None else None
    if target is None:
        return {"status": "missing", "message": "用户不存在"}
    if not verify_password(old_password, target.password_hash):
        return {"status": "invalid", "message": "原密码不正确"}
    problem = password_problem(new_password)
    if problem:
        return {"status": "invalid", "message": problem}
    if old_password == new_password:
        return {"status": "invalid", "message": "新密码不能与原密码相同"}
    target.password_hash = hash_password(new_password)
    target.must_change_password = False
    session.commit()
    revoke_user_sessions(session, target.id)
    return {"status": "changed"}


def delete_user(session, user_id: int, *, actor_id: int | None = None) -> dict[str, Any]:
    user = session.get(User, user_id)
    if user is None:
        return {"status": "missing"}
    if user.role == "admin" and _count_admins(session) <= 1:
        return {"status": "conflict", "message": "至少保留一个管理员"}
    if actor_id == user.id:
        return {"status": "conflict", "message": "不能删除自己的账号"}
    session.delete(user)
    session.commit()
    return {"status": "deleted", "id": user_id}


def _count_admins(session) -> int:
    return int(
        session.execute(
            select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))
        ).scalar_one()
    )


def user_by_username(session, username: str) -> User | None:
    return session.execute(select(User).where(User.username == username)).scalars().first()
