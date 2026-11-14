"""安全基础设施：密码哈希、会话令牌、角色层级、限流。

为什么全部用标准库
----------------
企业内网常常装不了新包（代理、审计、白名单），而这里需要的三件事标准库都能做：
``hashlib.scrypt``（密码）、``secrets`` + ``hmac.compare_digest``（令牌）、
``collections.deque``（限流窗口）。少一个依赖就少一个上线卡点。

密码存储格式：``scrypt$n$r$p$salt_hex$hash_hex``
* 参数写进字符串里，将来调参可以按用户平滑升级（识别到旧参数就重算）；
* 用 ``compare_digest`` 比较，避免时序侧信道。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from typing import Any

# --------------------------------------------------------------------------- #
# 角色：数值越大权限越高（比较大小即可判断「是否够用」）
# --------------------------------------------------------------------------- #
ROLE_LEVELS: dict[str, int] = {
    "viewer": 1,
    "editor": 2,
    "approver": 3,
    "admin": 4,
}

ROLE_LABELS: dict[str, str] = {
    "viewer": "只读",
    "editor": "编辑",
    "approver": "审批",
    "admin": "管理员",
}

ROLE_DESCRIPTIONS: dict[str, str] = {
    "viewer": "可浏览 Wiki、图谱、问答与 Agent 运行记录，不能改动任何数据",
    "editor": "可入库文档、建类型、编辑 Wiki（仍需 approver 确认提案）",
    "approver": "可确认/驳回提案，触发 Wiki 编译与 IM 通知",
    "admin": "全部权限：用户管理、组件切换、审计、备份、运维诊断",
}

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
DK_LEN = 32


def role_level(role: str) -> int:
    return ROLE_LEVELS.get((role or "").strip(), 0)


def has_role(role: str, minimum: str) -> bool:
    """``role`` 是否达到 ``minimum`` 要求。未知角色一律不通过。"""
    return role_level(role) >= role_level(minimum) > 0


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role or "未知")


# --------------------------------------------------------------------------- #
# 密码
# --------------------------------------------------------------------------- #
def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=DK_LEN
    )
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常量时间校验；存储串损坏时返回 False 而不是抛错。"""
    if not password or not stored:
        return False
    try:
        scheme, n_raw, r_raw, p_raw, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_raw),
            r=int(r_raw),
            p=int(p_raw),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def password_problem(password: str) -> str:
    """返回不合规原因；空串表示通过。企业最小强度要求。"""
    if len(password or "") < 8:
        return "密码至少 8 位"
    if password.isdigit():
        return "密码不能是纯数字"
    if password.lower() in {"password", "12345678", "admin123", "qwertyui"}:
        return "密码过于常见"
    return ""


# --------------------------------------------------------------------------- #
# 会话令牌
# --------------------------------------------------------------------------- #
def new_session_token() -> str:
    """返回明文令牌（只发给浏览器），库里只存它的哈希。"""
    return secrets.token_urlsafe(32)


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 限流（进程内滑动窗口；多副本部署时应换成 Redis）
# --------------------------------------------------------------------------- #
class RateLimiter:
    """按 key 的滑动窗口计数。

    key 选择：已登录用 ``user:<id>``，未登录用 ``ip:<addr>``。
    这样同一个 NAT 后面的多个用户不会互相拖累，而单个用户狂刷会被拦住。
    """

    def __init__(self, limit: int = 120, window_seconds: int = 60) -> None:
        self.limit = max(1, int(limit))
        self.window = max(1, int(window_seconds))
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        """返回 (是否放行, 建议的 Retry-After 秒数)。"""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry_after = max(1, int(self.window - (now - bucket[0])) + 1)
                return False, retry_after
            bucket.append(now)
            # 顺手清理空桶，避免长期运行内存膨胀
            if len(self._hits) > 4096:
                for stale in [k for k, v in self._hits.items() if not v]:
                    self._hits.pop(stale, None)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"keys": len(self._hits), "limit_per_window": self.limit, "window_s": self.window}


# --------------------------------------------------------------------------- #
# 客户端信息
# --------------------------------------------------------------------------- #
def client_ip(request) -> str:
    """取真实客户端 IP：反代场景优先 X-Forwarded-For 的第一跳。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip", "")
    if real_ip:
        return real_ip.strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or "unknown"


def user_agent(request) -> str:
    return (request.headers.get("user-agent", "") or "")[:200]
