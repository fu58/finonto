"""运维自检：把「上线前必须确认的事」做成可执行检查。

同一个模块被三处复用：
* ``GET /api/ready``       —— 就绪探针（只跑关键项）
* ``GET /api/system/diagnostics`` —— 运维页面与技术支持用（全量）
* ``scripts/preflight.py`` —— 上线前命令行核对（退出码非 0 即视为不可上线）

每项返回 ``{key, title, level, status, detail, hint}``：
level = critical / warning / info；status = ok / warn / fail / skip。
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import func, select

from ..config import Settings, get_settings
from ..db import get_engine, session_scope
from ..models import (
    AuditLog,
    ChangeProposal,
    Document,
    DocumentChunk,
    ObjectType,
    User,
    WikiPage,
)
from ..providers.registry import get_components


@dataclass
class Check:
    key: str
    title: str
    level: str = "critical"

    def ok(self, detail: str = "") -> dict[str, Any]:
        return {"key": self.key, "title": self.title, "level": self.level,
                "status": "ok", "detail": detail, "hint": ""}

    def warn(self, detail: str, hint: str = "") -> dict[str, Any]:
        return {"key": self.key, "title": self.title, "level": self.level,
                "status": "warn", "detail": detail, "hint": hint}

    def fail(self, detail: str, hint: str = "") -> dict[str, Any]:
        return {"key": self.key, "title": self.title, "level": self.level,
                "status": "fail", "detail": detail, "hint": hint}


def _python() -> dict[str, Any]:
    check = Check("python", "Python 版本")
    major, minor = sys.version_info[:2]
    detail = f"{platform.python_version()} ({'%s' % platform.platform()})"
    if (major, minor) < (3, 11):
        return check.fail(detail, "需要 Python 3.11+（3.12 已实测）")
    return check.ok(detail)


def _deps() -> dict[str, Any]:
    check = Check("deps", "运行依赖")
    versions = {}
    for module in ("fastapi", "sqlalchemy", "jinja2", "numpy", "uvicorn"):
        try:
            mod = __import__(module)
            versions[module] = getattr(mod, "__version__", "?")
        except ImportError:
            return check.fail(f"缺少 {module}", "pip install -r server/requirements.txt")
    detail = " · ".join(f"{k}={v}" for k, v in versions.items())
    return check.ok(detail)


def _data_dir() -> dict[str, Any]:
    check = Check("data_dir", "数据目录可写")
    settings = get_settings()
    settings.ensure_dirs()
    probe = settings.data_path / ".write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return check.fail(f"{settings.data_path}: {exc}", "检查目录权限或换 FINONTO_DATA_DIR")
    usage = shutil.disk_usage(settings.data_path)
    free_gb = round(usage.free / 1024 ** 3, 1)
    if free_gb < 1:
        return check.warn(f"剩余 {free_gb}GB", "磁盘空间不足，备份与上传可能失败")
    return check.ok(f"{settings.data_path} · 剩余 {free_gb}GB")


def _database() -> dict[str, Any]:
    check = Check("database", "数据库连接")
    try:
        with session_scope() as session:
            spaces = int(session.execute(select(func.count(ObjectType.id))).scalar_one())
        settings = get_settings()
        backend = "sqlite" if settings.resolved_database_url().startswith("sqlite") else "external"
        return check.ok(f"{backend} · 对象类型 {spaces} 个")
    except Exception as exc:  # noqa: BLE001
        return check.fail(str(exc), "检查 FINONTO_DATABASE_URL 与磁盘权限")


def _vectors() -> dict[str, Any]:
    check = Check("vectors", "向量维度一致性")
    settings = get_settings()
    components = get_components()
    try:
        stats = components.vector_store.stats()
        dim = getattr(components.embedding, "dim", 0)
        vectors = int(stats.get("vectors", 0))
        if vectors and stats.get("dim") and int(stats["dim"]) != int(dim):
            return check.fail(
                f"向量库 {stats['dim']} 维 / 当前 embedding {dim} 维",
                "换过 embedding 模型：到「文档入库」逐篇重建索引",
            )
        return check.ok(f"{vectors} 条向量 · {dim} 维 · {stats.get('backend', '')}")
    except Exception as exc:  # noqa: BLE001
        return check.fail(str(exc), "检查向量库配置")


def _auth() -> dict[str, Any]:
    check = Check("auth", "认证与初始密码", level="warning")
    settings = get_settings()
    if not settings.auth_enabled:
        return check.warn(
            "认证已关闭（演示模式）",
            "对外暴露前设置 FINONTO_AUTH_ENABLED=true，并修改初始管理员密码",
        )
    try:
        with session_scope() as session:
            users = int(session.execute(select(func.count(User.id))).scalar_one())
            admins = int(
                session.execute(
                    select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))
                ).scalar_one()
            )
            must_change = int(
                session.execute(
                    select(func.count(User.id)).where(User.must_change_password.is_(True))
                ).scalar_one()
            )
    except Exception as exc:  # noqa: BLE001
        return check.fail(str(exc), "检查 users 表是否已创建")
    if admins == 0:
        if users == 0:
            return check.warn(
                "还没有任何用户",
                "启动服务时会自动创建初始管理员（FINONTO_ADMIN_USERNAME / FINONTO_ADMIN_PASSWORD）",
            )
        return check.fail("没有启用的管理员账号", "用 FINONTO_ADMIN_PASSWORD 重置，或手工恢复 users 表")
    if must_change:
        return check.warn(
            f"{users} 个用户，其中 {must_change} 个仍用初始密码",
            "登录后到「账号设置」修改密码",
        )
    return check.ok(f"{users} 个用户 · {admins} 个管理员")


def _samples() -> dict[str, Any]:
    check = Check("samples", "领域包示例语料", level="warning")
    from ..core.ontology import packs

    missing = []
    for pack in packs.real_packs():
        for sample in pack.samples:
            if not sample.exists(pack.key):
                missing.append(f"{pack.key}/{sample.filename}")
    if missing:
        return check.warn(
            f"缺失 {len(missing)} 份：" + "、".join(missing[:3]),
            "示例语料缺失不影响运行，只是安装领域包时不会入库对应文档",
        )
    return check.ok(f"{len(packs.real_packs())} 个领域包语料齐全")


def _sandbox() -> dict[str, Any]:
    check = Check("sandbox", "代码沙箱", level="warning")
    settings = get_settings()
    if not settings.sandbox_enabled:
        return check.warn("沙箱已关闭", "Agent 的 sandbox.run_python 工具不可用")
    from ..core.agent import sandbox

    allowed = sandbox.validate("print(1)")
    blocked = sandbox.validate("import os")
    if not allowed.get("ok"):
        return check.fail("白名单代码被误拦", str(allowed.get("blocked")))
    if blocked.get("ok"):
        return check.fail("危险导入未被拦截", "沙箱规则可能被改动，请检查 core/agent/sandbox.py")
    return check.ok("白名单放行 / 越权拦截 均正常")


def _content() -> dict[str, Any]:
    check = Check("content", "内容规模与待办", level="info")
    try:
        with session_scope() as session:
            documents = int(session.execute(select(func.count(Document.id))).scalar_one())
            chunks = int(session.execute(select(func.count(DocumentChunk.id))).scalar_one())
            pages = int(session.execute(select(func.count(WikiPage.id))).scalar_one())
            dirty = int(
                session.execute(select(func.count(WikiPage.id)).where(WikiPage.is_dirty.is_(True))).scalar_one()
            )
            pending = int(
                session.execute(
                    select(func.count(ChangeProposal.id)).where(ChangeProposal.status == "pending")
                ).scalar_one()
            )
    except Exception as exc:  # noqa: BLE001
        return check.warn(str(exc))
    detail = f"文档 {documents} · 片段 {chunks} · Wiki 页 {pages} · 待编译 {dirty} · 待确认提案 {pending}"
    if dirty or pending:
        return check.warn(detail, "有未编译页面或未确认提案，属于正常运营状态")
    return check.ok(detail)


def _audit() -> dict[str, Any]:
    check = Check("audit", "审计日志", level="warning")
    settings = get_settings()
    try:
        with session_scope() as session:
            total = int(session.execute(select(func.count(AuditLog.id))).scalar_one())
    except Exception as exc:  # noqa: BLE001
        return check.fail(str(exc), "检查 audit_logs 表")
    if not settings.auth_enabled:
        return check.warn(f"{total} 条（认证关闭，审计归因不可靠）")
    if total == 0:
        return check.warn("还没有审计记录", "登录一次后应有 login 记录")
    return check.ok(f"{total} 条，保留 {settings.audit_retention_days} 天")


def _secrets() -> dict[str, Any]:
    check = Check("secrets", "密钥与外部依赖", level="info")
    settings = get_settings()
    flags = settings.secret_flags()
    configured = [key for key, value in flags.items() if value]
    if settings.llm_provider == "mock":
        return check.ok("零外呼模式（mock / local_hash / sqlite_numpy）")
    return check.warn(
        f"已配置 {len(configured)} 项密钥 · llm={settings.llm_provider}",
        "确认外呼目标在安全白名单内",
    )


ALL_CHECKS: tuple[Callable[[], dict[str, Any]], ...] = (
    _python, _deps, _data_dir, _database, _vectors, _auth, _audit, _samples, _sandbox, _content, _secrets,
)

READY_CHECKS = ("database", "vectors")


def run_checks() -> dict[str, Any]:
    results = [check() for check in ALL_CHECKS]
    counts = {"ok": 0, "warn": 0, "fail": 0}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    # 任何 fail 都算阻塞（哪怕该检查的 level 是 warning）—— 状态与结论不能打架
    failures = [item for item in results if item["status"] == "fail"]
    return {
        "checks": results,
        "counts": counts,
        "ready": not failures,
        "blocking": [item["key"] for item in failures],
        "generated_at": _now(),
    }


def readiness() -> dict[str, Any]:
    """就绪探针：只跑关键项，任何一个失败即视为未就绪。"""
    results = []
    for item in ALL_CHECKS:
        if item.__name__.strip("_") in READY_CHECKS:
            results.append(item())
    ready = all(item["status"] == "ok" for item in results)
    return {"ready": ready, "checks": results, "generated_at": _now()}


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def summary_line(result: dict[str, Any]) -> str:
    counts = result.get("counts", {})
    return f"ok={counts.get('ok', 0)} warn={counts.get('warn', 0)} fail={counts.get('fail', 0)}"
