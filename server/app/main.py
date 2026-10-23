"""FinOnto HTTP 服务入口。

启动顺序：建表 → 幂等冷启动（本体基线 + 示例语料 + 首次 Wiki 编译）→ 注册路由。
`FINONTO_AUTO_BOOTSTRAP=false` 可跳过冷启动；构建后的前端目录存在时自动托管，
于是本地浏览器打开一个端口就能用。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api import agent, components, ingest, ontology, rag, system, wiki
from .config import SERVER_DIR, get_settings
from .db import init_db, session_scope
from .services import bootstrap

settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("finonto")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if settings.auto_bootstrap:
        try:
            with session_scope() as session:
                report = bootstrap.bootstrap(session, SERVER_DIR)
            log.info("冷启动完成：%s", report.get("counters"))
        except Exception as exc:  # noqa: BLE001  冷启动失败不应该阻止服务启动
            log.warning("冷启动跳过：%s", exc)
    else:
        log.info("FINONTO_AUTO_BOOTSTRAP=false，跳过冷启动")
    yield


app = FastAPI(
    title=settings.app_name,
    description="多领域本体生成平台：可插拔领域包（金融 / 设备维保 / 人工智能 / 计算机网络）+ RAG 问答 + ReAct Agent 推理 + 本体提案（人工确认）+ 自动 Wiki 与知识图谱。",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (system, ontology, rag, ingest, agent, wiki, components):
    app.include_router(module.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    return {"status": "ok", "app": settings.app_name, "version": app.version}


# 前端构建产物存在时一并托管（本地浏览器单端口访问）
_client_dist = SERVER_DIR.parent / "client" / "dist"
if _client_dist.exists():
    app.mount("/", StaticFiles(directory=str(_client_dist), html=True), name="client")
