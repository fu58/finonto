"""安全相关单元测试：密码、令牌、角色、限流、审计脱敏、诊断。

    cd server
    python -m pytest ../tests -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))
os.environ.setdefault("FINONTO_DATA_DIR", str(SERVER_DIR / "var-test"))

from app import security  # noqa: E402
from app.core import audit, diagnostics  # noqa: E402
from app.observability import Metrics  # noqa: E402


# --------------------------------------------------------------------------- #
# 密码
# --------------------------------------------------------------------------- #
def test_password_hash_roundtrip():
    stored = security.hash_password("Adm1n-Passw0rd!")
    assert stored.startswith("scrypt$")
    assert security.verify_password("Adm1n-Passw0rd!", stored) is True
    assert security.verify_password("wrong", stored) is False


def test_password_hash_is_salted():
    first = security.hash_password("same-password")
    second = security.hash_password("same-password")
    assert first != second


def test_verify_handles_corrupt_hash():
    assert security.verify_password("x", "not-a-hash") is False
    assert security.verify_password("x", "") is False
    assert security.verify_password("", "scrypt$1$1$1$aa$bb") is False


def test_password_strength_rules():
    assert security.password_problem("short") != ""
    assert security.password_problem("12345678") != ""
    assert security.password_problem("admin123") != ""
    assert security.password_problem("Adm1n-Passw0rd!") == ""


# --------------------------------------------------------------------------- #
# 令牌
# --------------------------------------------------------------------------- #
def test_session_token_is_random_and_hashed():
    first = security.new_session_token()
    second = security.new_session_token()
    assert first != second and len(first) > 30
    fingerprint = security.token_fingerprint(first)
    assert len(fingerprint) == 64 and fingerprint != first


# --------------------------------------------------------------------------- #
# 角色
# --------------------------------------------------------------------------- #
def test_role_hierarchy():
    assert security.has_role("admin", "approver") is True
    assert security.has_role("approver", "admin") is False
    assert security.has_role("editor", "viewer") is True
    assert security.has_role("viewer", "editor") is False


def test_unknown_role_never_passes():
    assert security.has_role("", "viewer") is False
    assert security.has_role("superuser", "viewer") is False
    assert security.role_level("nope") == 0


def test_role_labels_cover_all_levels():
    for role in security.ROLE_LEVELS:
        assert security.role_label(role) != role


# --------------------------------------------------------------------------- #
# 限流
# --------------------------------------------------------------------------- #
def test_rate_limiter_blocks_after_limit():
    limiter = security.RateLimiter(limit=3, window_seconds=60)
    results = [limiter.allow("u:1")[0] for _ in range(5)]
    assert results[:3] == [True, True, True]
    assert results[3] is False
    allowed, retry_after = limiter.allow("u:1")
    assert allowed is False and retry_after >= 1


def test_rate_limiter_is_per_key():
    limiter = security.RateLimiter(limit=2, window_seconds=60)
    limiter.allow("a")
    limiter.allow("a")
    assert limiter.allow("a")[0] is False
    assert limiter.allow("b")[0] is True


# --------------------------------------------------------------------------- #
# 审计脱敏
# --------------------------------------------------------------------------- #
def test_audit_sanitize_masks_secrets():
    cleaned = audit._sanitize(
        {
            "api_key": "sk-real-secret",
            "password": "p@ss",
            "webhook": "https://hook/xxx",
            "note": "正常内容",
            "long": "x" * 900,
        }
    )
    assert cleaned["api_key"] == "***"
    assert cleaned["password"] == "***"
    assert cleaned["webhook"] == "***"
    assert cleaned["note"] == "正常内容"
    assert len(cleaned["long"]) == 400


def test_audit_action_labels_are_chinese():
    for action in audit.ACTIONS:
        assert audit.action_label(action) != ""
    assert audit.action_label("accept_proposal") == "确认提案"
    assert audit.status_label("denied") == "被拒绝"


# --------------------------------------------------------------------------- #
# 指标
# --------------------------------------------------------------------------- #
def test_metrics_bucket_collapses_ids():
    from app.observability import _bucket

    assert _bucket("/api/wiki/pages/12") == "/api/wiki/pages/{id}"
    assert _bucket("/") == "/"
    assert _bucket("/api/spaces/3/types/9") == "/api/spaces/{id}/types/{id}"


def test_metrics_observe_counts_errors():
    metrics = Metrics()
    metrics.observe(method="GET", path="/", status=200, elapsed_ms=5)
    metrics.observe(method="POST", path="/api/x", status=500, elapsed_ms=1500)
    snapshot = metrics.snapshot()
    assert snapshot["requests_total"] == 2
    assert snapshot["errors_total"] == 1
    assert snapshot["slow_requests"] == 1
    assert snapshot["latency_ms_max"] >= 1500


# --------------------------------------------------------------------------- #
# 诊断
# --------------------------------------------------------------------------- #
def test_diagnostics_returns_structured_checks():
    result = diagnostics.run_checks()
    assert set(result["counts"]) == {"ok", "warn", "fail"}
    assert isinstance(result["checks"], list) and result["checks"]
    for item in result["checks"]:
        assert {"key", "title", "level", "status", "detail"} <= set(item)
        assert item["status"] in ("ok", "warn", "fail")
    assert "ready" in result and "blocking" in result


def test_readiness_only_runs_key_checks():
    result = diagnostics.readiness()
    keys = {item["key"] for item in result["checks"]}
    assert keys == {"database", "vectors"}
