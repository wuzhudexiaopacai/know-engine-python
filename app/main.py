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

    # 数据库：探测引擎（MySQL→SQLite 降级）→ 开发态建表 → 租户隔离钩子 → 幂等种子
    from app.core.database import init_database
    await init_database()
    from app.models.seed import seed_if_needed
    await seed_if_needed()

    # 文档流水线监听器（事件驱动：上传→转换→切片→向量化）
    document_pipeline.register_listeners()
    # 事件总线：Redis 可用时走 Stream 消费组（多实例共享队列），否则进程内分发
    from app.infra.event_bus import get_event_bus
    await get_event_bus().start()

    # 定时补偿任务（多实例下由分布式锁保证单活，可用 SCHEDULER__ENABLED 关闭）
    scheduler = compensate.build_scheduler() if settings.scheduler.enabled else None
    if scheduler is not None:
        scheduler.start()
        logger.info("scheduler_started")

    yield

    # 优雅关闭：先停调度器（不再产生新事件），再停事件消费
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
        except Exception:  # pragma: no cover
            pass
    try:
        from app.infra.event_bus import get_event_bus
        await get_event_bus().stop()
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


# ---------------- 就绪探针 ----------------
async def _probe_mysql() -> dict:
    try:
        from sqlalchemy import text

        from app.core.database import session_scope
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "UP"}
    except Exception as e:
        return {"status": "DOWN", "error": str(e)[:200]}


async def _probe_redis() -> dict:
    """未配置时视为可选项（SKIPPED）：进程内降级是设计能力，不应阻塞就绪。"""
    url = settings.redis.url
    if not url:
        return {"status": "SKIPPED", "reason": "未配置，进程内降级"}
    try:
        import redis.asyncio as aioredis
        client = aioredis.from_url(url, socket_connect_timeout=1.0, socket_timeout=1.0)
        try:
            await client.ping()
        finally:
            await client.aclose()
        return {"status": "UP"}
    except Exception as e:
        return {"status": "DOWN", "error": str(e)[:200]}


async def _probe_elasticsearch() -> dict:
    cfg = settings.vector
    if not cfg.es_url:
        return {"status": "SKIPPED", "reason": "未配置，降级内存向量存储"}
    try:
        from elasticsearch import AsyncElasticsearch
        kwargs = {"hosts": cfg.es_url, "request_timeout": 2.0}
        if cfg.es_username:
            kwargs["basic_auth"] = (cfg.es_username, cfg.es_password)
        client = AsyncElasticsearch(**kwargs)
        try:
            ok = await client.ping()
        finally:
            await client.close()
        return {"status": "UP" if ok else "DOWN"}
    except Exception as e:
        return {"status": "DOWN", "error": str(e)[:200]}


async def _probe_storage() -> dict:
    from starlette.concurrency import run_in_threadpool

    from app.infra.file_storage import get_file_store
    store = get_file_store()
    if type(store).__name__ == "MinioFileStore":
        try:
            await run_in_threadpool(store._client.bucket_exists, store.bucket_default)
            return {"status": "UP", "backend": "minio"}
        except Exception as e:
            return {"status": "DOWN", "backend": "minio", "error": str(e)[:200]}
    return {"status": "UP", "backend": "local"}


@app.get("/api/health/ready")
async def health_ready():
    """就绪探针：探测 MySQL/Redis/ES/对象存储依赖，任一必需依赖失败返回 503。

    Redis/ES 未配置属于设计内降级（SKIPPED），不影响就绪判定；
    供 K8s readinessProbe / 负载均衡健康检查使用。
    """
    import asyncio as _asyncio

    mysql, redis_, es, storage = await _asyncio.gather(
        _probe_mysql(), _probe_redis(), _probe_elasticsearch(), _probe_storage())
    checks = {"mysql": mysql, "redis": redis_, "elasticsearch": es, "storage": storage}
    ready = all(c["status"] != "DOWN" for c in checks.values())
    # 事件队列积压仅作信息展示（积压偏高是运行时信号，不应摘除实例）
    from app.infra.event_bus import get_event_bus
    events = await get_event_bus().stats()
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=200 if ready else 503,
                        content={"success": ready, "ready": ready, "checks": checks,
                                 "events": events})


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
