"""FinOnto 应用入口。

一个进程做四件事：提供 API、渲染页面、托管静态资源、执行请求级中间件
（认证 / 授权 / 限流 / 审计归因 / 指标）。所以「一条命令」就足够：

    python -m app.main
    # 或 scripts/dev.ps1 / scripts/dev.sh

冷启动（``FINONTO_AUTO_BOOTSTRAP=true``）：
建表 → 创建初始管理员（仅当没有用户）→ 若一个空间都没有则装入全部内置领域包。

企业上线前请跑 ``python scripts/preflight.py``，它把「必须确认的事」做成了可执行检查。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import middleware as app_middleware
from .api import agent, auth as auth_api, components, ingest, ontology, ops, rag, system, wiki
from .config import get_settings
from .core import audit, auth_service
from .core.ontology import packs, service
from .db import init_db, session_scope
from .providers.registry import get_components
from .web import pages_auth
from .web.pages import router as pages_router
from .web.render import STATIC_DIR, render_error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("finonto")


def bootstrap_if_empty() -> dict:
    """幂等冷启动：管理员只建一次；领域包只在没有任何空间时装。"""
    settings = get_settings()
    with session_scope() as session:
        admin = auth_service.ensure_admin(session, settings)
        if admin.get("status") == "created":
            logger.info(
                "已创建初始管理员 %s（需首次登录后改密）", admin.get("username")
            )
        auth_service.cleanup_expired_sessions(session)

        existing = service.list_spaces(session)
        if existing:
            return {"status": "skipped", "spaces": len(existing), "admin": admin.get("status")}

        results = []
        for pack in packs.all_packs():
            result = service.install_pack(
                session, pack.key, components=get_components(), settings=settings
            )
            results.append(result)
            logger.info(
                "领域包装入 %s：类型 %s，关联 %s，文档 %s 份，Wiki %s 页",
                pack.key,
                result.get("created_types"),
                result.get("created_links"),
                len(result.get("documents") or []),
                (result.get("wiki") or {}).get("pages", 0),
            )
        audit.record(
            session,
            action="install_pack",
            target="bootstrap:all",
            detail={"packs": [item.get("pack") for item in results]},
            actor="system",
        )
        session.commit()
        return {"status": "bootstrapped", "packs": results, "admin": admin.get("status")}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    init_db()
    logger.info("数据库：%s", settings.resolved_database_url())
    logger.info("数据目录：%s", settings.data_path)
    logger.info(
        "组件：llm=%s embedding=%s vector=%s", settings.llm_provider, settings.embedding_provider, settings.vector_store
    )
    logger.info(
        "安全：auth=%s 限流=%s/min 会话=%sh 审计保留=%s天",
        "on" if settings.auth_enabled else "off(演示模式)",
        settings.rate_limit_per_min,
        settings.session_ttl_hours,
        settings.audit_retention_days,
    )
    if settings.auth_enabled and settings.admin_password == "admin123":
        logger.warning("管理员仍使用默认密码 admin123 —— 上线前必须修改（FINONTO_ADMIN_PASSWORD）")
    if settings.auto_bootstrap:
        try:
            result = bootstrap_if_empty()
            logger.info("冷启动：%s", result.get("status"))
        except Exception as exc:  # noqa: BLE001 - 冷启动失败不应阻止服务起来
            logger.exception("冷启动失败：%s", exc)
    yield


settings = get_settings()

app = FastAPI(
    title="FinOnto · 多领域本体生成平台",
    version="0.2.0",
    description="RAG 问答 + Agent 推理 → 引导式本体生成 → 可编辑可回滚的 Wiki 与知识图谱"
    "（含认证、RBAC、审计、可观测性、备份与上线自检）",
    lifespan=lifespan,
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# 请求级中间件：request-id → 会话 → 认证 → CSRF → 授权 → 限流 → 指标/日志
app.middleware("http")(app_middleware.handle)

# API 路由
app.include_router(system.router)
app.include_router(auth_api.router)
app.include_router(ontology.router)
app.include_router(rag.router)
app.include_router(ingest.router)
app.include_router(agent.router)
app.include_router(wiki.router)
app.include_router(components.router)
app.include_router(ops.router)

# 页面路由（在 API 之后注册，避免 /api 前缀被页面通配符吃掉）
app.include_router(pages_router)
app.include_router(pages_auth.router)

# 静态资源（不需要登录）
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith("/api/"):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "接口不存在", "title": "404"}, status_code=404)
    return render_error(request, 404, "页面不存在", f"路径：{request.url.path}")


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False, log_level="info")


if __name__ == "__main__":
    main()
