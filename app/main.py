"""FastAPI 应用入口：生命周期装配 + 路由挂载 + CORS + 静态前端托管。

启动顺序（lifespan）：
    结构化日志 → 数据库(探测/建表/种子) → 注册文档流水线监听器 → 启动定时补偿任务
关闭顺序：
    停止调度器 → 关闭 LLM/Embedding/Rerank 连接池
"""

from __future__ import annotations

import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config.settings import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import get_logger, setup_logging
from app.core.response import R
from app.jobs import compensate
from app.modules.admin import controller as admin_controller
from app.modules.auth import controller as auth_controller
from app.modules.chat import controller as chat_controller
from app.modules.document import controller as document_controller
from app.modules.document import pipeline as document_pipeline
from app.modules.qa_cache import controller as qa_cache_controller

logger = get_logger("app.main")

settings = get_settings()
_WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("app_starting", name=settings.app.name, env=settings.app.env)

    # 数据库：探测引擎（MySQL→SQLite 降级）→ 开发态建表 → 幂等种子
    from app.core.database import init_database
    await init_database()
    from app.models.seed import seed_if_needed
    await seed_if_needed()

    # 文档流水线监听器（事件驱动：上传→转换→切片→向量化）
    document_pipeline.register_listeners()

    # 定时补偿任务
    scheduler = compensate.build_scheduler()
    scheduler.start()
    logger.info("scheduler_started")

    yield

    # 优雅关闭
    try:
        scheduler.shutdown(wait=False)
    except Exception:  # pragma: no cover
        pass
    from app.services.embedding import close_embedding
    from app.services.image import close_image
    from app.services.llm_client import close_llm
    from app.services.rerank import get_rerank
    from app.services.speech import close_speech
    await close_llm()
    await close_embedding()
    await get_rerank().close()
    await close_speech()
    await close_image()
    logger.info("app_stopped")


app = FastAPI(
    title="KnowEngine-Python 企业级智能知识引擎",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.app.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

# ---------------- REST 路由 ----------------
app.include_router(auth_controller.router)
app.include_router(document_controller.router)
app.include_router(chat_controller.router)
app.include_router(qa_cache_controller.router)
app.include_router(admin_controller.router)


@app.get("/api/health", response_model=R)
async def health() -> R:
    """存活探针（前端与运维共用）。"""
    return R.ok({"status": "UP", "env": settings.app.env})


@app.get("/api/files/{key:path}")
async def read_stored_file(key: str):
    """对象存储文件回读（免登录只读）：头像等需要长期稳定 URL 的场景。

    统一走对象存储抽象：本地为磁盘文件，MinIO 自动下载到本地缓存；
    key 为 uuid 派生，不可猜测，但仍校验路径穿越。
    """
    if not key or key.startswith("/") or ".." in key:
        raise HTTPException(status_code=400, detail="非法文件路径")
    from app.infra.file_storage import get_file_store
    path = await get_file_store().get(key)
    media_type, _ = mimetypes.guess_type(key)
    return FileResponse(path, media_type=media_type or "application/octet-stream")


# ---------------- 静态前端（Vue3 构建产物）托管 ----------------
if _WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=_WEB_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        """SPA 兜底：非 /api 路径一律回 index.html；命中静态资源直接回文件。"""
        file = _WEB_DIST / full_path
        if full_path and file.is_file():
            return FileResponse(file)
        return FileResponse(_WEB_DIST / "index.html")
else:
    @app.get("/")
    async def root():
        return {"name": settings.app.name, "docs": "/api/docs",
                "hint": "前端未构建：cd web && npm install && npm run build"}
