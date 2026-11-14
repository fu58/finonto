"""请求级中间件：request-id、会话解析、认证闸门、限流、访问日志、指标。

顺序很重要
----------
1. 先发 request-id（后面所有日志与审计都带它，便于串起一次操作）；
2. 再解析会话（失败不拦路，只是当作未登录）；
3. 再判认证（公开路径放行，API 返回 401，页面跳登录）；
4. 再限流（只对贵接口：RAG / Agent，避免把静态资源也限进去）；
5. 最后记指标与日志（无论成功失败）。

认证可关闭
----------
``FINONTO_AUTH_ENABLED=false`` 时视为「内网可信用户」模式（演示/离线部署常见），
此时所有请求按 admin 处理，页面顶部会显示明显的「演示模式」提示，避免误以为有鉴权。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from .config import get_settings
from .core.audit import record as audit_record
from .core.auth_service import resolve_session
from .db import session_scope
from .models import User
from .observability import METRICS
from .security import RateLimiter, client_ip, has_role, user_agent

logger = logging.getLogger("finonto.access")

SESSION_COOKIE = "finonto_session"
CSRF_HEADER = "x-finonto-csrf"

#: 不需要登录即可访问的路径前缀
PUBLIC_PREFIXES = (
    "/static/",
    "/login",
    "/logout",
    "/favicon.ico",
    "/api/health",
    "/api/ready",
    "/api/metrics",
    "/api/auth/login",
)

#: 需要限流的昂贵接口前缀
RATE_LIMITED_PREFIXES = ("/api/rag/", "/api/agent/")

#: 只读方法
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


class DemoUser:
    """认证关闭时注入的伪用户（不落库，只用于模板与角色判断）。"""

    id = 0
    username = "demo"
    display_name = "演示模式"
    role = "admin"
    is_active = True
    must_change_password = False


_LIMITER: RateLimiter | None = None

# --------------------------------------------------------------------------- #
# 授权矩阵：路径 + 方法 → 最低角色（顺序敏感，具体规则放前面）
# --------------------------------------------------------------------------- #
WRITE_RULES: tuple[tuple[str, str, str], ...] = (
    # 提案确认 / 驳回：只给审批者（硬闸门）
    ("POST", "/api/ontology/proposals/", "approver"),
    ("POST", "/proposals/", "approver"),
    # 领域包安装：管理员
    ("POST", "/api/ontology/packs/", "admin"),
    ("POST", "/packs/", "admin"),
    # 组件切换与探针：管理员
    ("GET", "/api/components", "admin"),
    ("POST", "/api/components", "admin"),
    ("POST", "/components/", "admin"),
    ("GET", "/components", "admin"),
    # 运维 / 用户 / 审计
    ("GET", "/api/system/diagnostics", "admin"),
    ("GET", "/api/system/backup", "admin"),
    ("POST", "/api/system/backup", "admin"),
    ("DELETE", "/api/system/backups", "admin"),
    ("POST", "/api/audit/purge", "admin"),
    ("GET", "/api/audit", "approver"),
    ("GET", "/api/auth/users", "admin"),
    ("POST", "/api/auth/users", "admin"),
    ("PATCH", "/api/auth/users", "admin"),
    ("DELETE", "/api/auth/users", "admin"),
    ("GET", "/ops", "admin"),
    ("GET", "/users", "admin"),
    ("GET", "/audit", "approver"),
    # 文档页含删除/重建索引操作 → 编辑者起
    ("GET", "/documents", "editor"),
    ("DELETE", "/api/documents", "editor"),
    # 本体写入：编辑者
    ("POST", "/api/ontology/", "editor"),
    ("PATCH", "/api/ontology/", "editor"),
    ("DELETE", "/api/ontology/", "editor"),
    ("POST", "/ontology/", "editor"),
    # Wiki 与文档：编辑者；回滚/编译属结构性操作，仍是编辑者（可审计）
    ("POST", "/api/wiki/", "editor"),
    ("POST", "/wiki/", "editor"),
    ("POST", "/api/documents", "editor"),
    ("DELETE", "/api/documents", "editor"),
    ("POST", "/documents", "editor"),
    # Agent 侧写入：记忆与沙箱
    ("POST", "/api/agent/memory", "editor"),
    ("POST", "/api/agent/sandbox", "editor"),
    ("POST", "/api/agent/mcp", "admin"),
    ("POST", "/api/agent/run", "editor"),
)


def required_role(method: str, path: str) -> str | None:
    for rule_method, prefix, role in WRITE_RULES:
        if method == rule_method and path.startswith(prefix):
            return role
    return None


def limiter() -> RateLimiter:
    global _LIMITER
    if _LIMITER is None:
        settings = get_settings()
        _LIMITER = RateLimiter(limit=settings.rate_limit_per_min, window_seconds=60)
    return _LIMITER


def is_public(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def current_user(request: Request) -> User | DemoUser | None:
    return getattr(request.state, "user", None)


def _wants_html(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return False
    accept = request.headers.get("accept", "")
    return "text/html" in accept or accept == "" or "mozilla" in request.headers.get("user-agent", "").lower()


def _error_page(request: Request, status: int, title: str, detail: str) -> Any:
    if not _wants_html(request):
        return JSONResponse({"detail": detail, "title": title}, status_code=status)
    from .web.render import render_error

    return render_error(request, status, title, detail)


async def handle(request: Request, call_next):
    settings = get_settings()
    started = time.perf_counter()
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    request.state.user = None
    request.state.auth_enabled = settings.auth_enabled

    # ---- 1. 解析会话 ----
    if settings.auth_enabled:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            try:
                with session_scope() as session:
                    user = resolve_session(session, token)
                    if user is not None:
                        session.expunge(user)
                        request.state.user = user
            except Exception as exc:  # noqa: BLE001 - 会话解析失败按未登录处理
                logger.warning("会话解析失败：%s", exc)
    else:
        request.state.user = DemoUser()

    user = request.state.user
    path = request.url.path

    # ---- 2. 认证闸门 ----
    if settings.auth_enabled and user is None and not is_public(path):
        if path.startswith("/api/"):
            return JSONResponse(
                {"detail": "未登录或会话已过期", "title": "401"}, status_code=401
            )
        target = f"/login?next={quote(str(request.url.path) + ('?' + request.url.query if request.url.query else ''))}"
        return RedirectResponse(target, status_code=303)

    # ---- 2.5 CSRF 兜底：跨站 unsafe 请求直接拒绝 ----
    # SameSite=Lax 已挡住绝大多数跨站 POST，这里再加一道 Origin/Host 比对，
    # 对抗「浏览器旧版本 / 同站子域被污染」的组合场景。
    if settings.auth_enabled and request.method not in SAFE_METHODS:
        origin = request.headers.get("origin", "")
        if origin:
            from urllib.parse import urlparse

            origin_host = urlparse(origin).netloc
            host = request.headers.get("host", "")
            if origin_host and host and origin_host != host:
                audit_record(
                    action="login_failed",
                    target=path,
                    status="denied",
                    detail={"reason": "csrf_origin_mismatch", "origin": origin[:120]},
                    request=request,
                )
                return _error_page(request, 403, "跨站请求被拒绝", "Origin 与 Host 不一致")

    # ---- 2.6 授权：路径级角色检查（与接口内 require_role 互为双保险）----
    if settings.auth_enabled and user is not None:
        minimum = required_role(request.method, path)
        if minimum and not has_role(getattr(user, "role", ""), minimum):
            audit_record(
                action="role_denied",
                target=path,
                status="denied",
                detail={"required": minimum, "actual": getattr(user, "role", ""), "method": request.method},
                request=request,
                actor=getattr(user, "username", ""),
                role=getattr(user, "role", ""),
            )
            return _error_page(
                request, 403, "权限不足", f"该操作需要 {minimum} 及以上角色（当前：{getattr(user, 'role', '')}）"
            )

    # ---- 2.7 强制改密：初始密码未改前只能访问账号设置 ----
    if settings.auth_enabled and user is not None and getattr(user, "must_change_password", False):
        allowed = (
            "/profile", "/logout", "/login", "/static",
            "/api/auth/change-password", "/api/auth/me", "/api/auth/logout",
            "/api/health", "/api/ready",
        )
        if not any(path.startswith(prefix) for prefix in allowed) and not is_public(path):
            if path.startswith("/api/"):
                return JSONResponse(
                    {"detail": "请先修改初始密码", "title": "403"}, status_code=403
                )
            return RedirectResponse("/profile", status_code=303)

    # ---- 3. 限流（只针对昂贵接口）----
    if any(path.startswith(prefix) for prefix in RATE_LIMITED_PREFIXES):
        key = f"user:{getattr(user, 'id', 0)}" if user else f"ip:{client_ip(request)}"
        allowed, retry_after = limiter().allow(key)
        if not allowed:
            audit_record(
                action="login_failed", target=path, status="denied",
                detail={"reason": "rate_limited", "limit": settings.rate_limit_per_min},
                request=request, actor=getattr(user, "username", "anonymous"),
            )
            return JSONResponse(
                {"detail": f"请求过于频繁，请 {retry_after} 秒后重试", "title": "429"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

    # ---- 4. 执行 ----
    try:
        response = await call_next(request)
    except Exception as exc:  # noqa: BLE001 - 未捕获异常也要留下可诊断的输出
        elapsed = (time.perf_counter() - started) * 1000
        METRICS.observe(method=request.method, path=path, status=500, elapsed_ms=elapsed)
        logger.exception("未捕获异常 request_id=%s path=%s", request_id, path)
        return _error_page(request, 500, "服务内部错误", f"{type(exc).__name__}: {exc}")

    elapsed = (time.perf_counter() - started) * 1000
    METRICS.observe(method=request.method, path=path, status=response.status_code, elapsed_ms=elapsed)
    response.headers["X-Request-ID"] = request_id

    logger.info(
        json.dumps(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "request_id": request_id,
                "method": request.method,
                "path": path,
                "status": response.status_code,
                "ms": round(elapsed, 1),
                "user": getattr(user, "username", "anonymous"),
                "role": getattr(user, "role", ""),
                "ip": client_ip(request),
                "ua": user_agent(request)[:80],
            },
            ensure_ascii=False,
        )
    )
    return response


# --------------------------------------------------------------------------- #
# 权限依赖（给 API 端点用）
# --------------------------------------------------------------------------- #
def require_role(minimum: str):
    """FastAPI 依赖：校验当前用户角色是否达到 ``minimum``。

    认证关闭时（演示模式）视为 admin，直接放行。
    用 ``HTTPException`` 而不是返回 Response —— 依赖的返回值不会短路请求，
    只有抛异常才会。
    """

    async def dependency(request: Request):
        from fastapi import HTTPException

        settings = get_settings()
        if not settings.auth_enabled:
            return request.state.user
        user = request.state.user
        if user is None:
            raise HTTPException(status_code=401, detail="未登录或会话已过期")
        if not has_role(getattr(user, "role", ""), minimum):
            audit_record(
                action="role_denied",
                target=str(request.url.path),
                status="denied",
                detail={"required": minimum, "actual": getattr(user, "role", "")},
                request=request,
            )
            raise HTTPException(status_code=403, detail=f"需要 {minimum} 及以上角色")
        return user

    return dependency


def require_user():
    """只要求登录（任意角色）。"""

    async def dependency(request: Request):
        from fastapi import HTTPException

        settings = get_settings()
        if not settings.auth_enabled:
            return request.state.user
        user = request.state.user
        if user is None:
            raise HTTPException(status_code=401, detail="未登录或会话已过期")
        return user

    return dependency
