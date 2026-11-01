"""数据库引擎与会话工厂。

中间件策略：主存储为 docker-compose 部署的 MySQL 8；
连接失败且配置允许降级时，自动切换本地 SQLite（开发体验优先），
并在日志中显著告警，避免开发者误把降级库当成真实数据。

工程细节：
- SQLAlchemy 2.0 async，DeclarativeBase 统一实体基类
- SQLite 场景注入 PRAGMA：WAL 模式（读写并发）、busy_timeout（防锁死）
- session_scope 提供事务边界，业务层禁止直接 commit
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config.settings import get_settings
from app.core.logging import get_logger

logger = get_logger("app.db")

_settings = get_settings()


class Base(DeclarativeBase):
    """所有 ORM 实体的基类。"""


def _create_engine(url: str):
    """按 DSN 类型创建异步引擎，并做差异化调优。"""
    kwargs: dict = {"echo": _settings.db.echo, "future": True}
    if not url.startswith("sqlite"):
        # MySQL/PG：连接池对齐 Druid 参数；pre_ping 防拿到已断连接
        kwargs.update(pool_size=_settings.db.pool_size, max_overflow=_settings.db.max_overflow,
                      pool_pre_ping=True, pool_recycle=3600)
    engine = create_async_engine(url, **kwargs)

    if url.startswith("sqlite"):
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragma(dbapi_conn, _):
            """SQLite 生产可用三件套：WAL、外键、5s 忙等待。"""
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def _ensure_sqlite_dir(url: str) -> None:
    """SQLite 降级库文件所在目录不存在时自动创建，避免首次启动因缺目录而报错。"""
    # 形如 sqlite+aiosqlite:///{绝对路径}；/// 后为文件路径
    if ":///" not in url:
        return
    file_path = url.split(":///", 1)[1]
    parent = Path(file_path).parent
    parent.mkdir(parents=True, exist_ok=True)


def _patch_aiomysql_bytes_escape() -> None:
    """兼容垫片：修复 aiomysql 与新 PyMySQL 的 bytes 转义不兼容。

    新版 pymysql.converters 将旧的 escape_bytes_prefixed 变为字符串占位
    （'DO NOT IMPORT THIS!!!'）并改名为 escape_bytes；aiomysql 的 Connection.escape
    仍把 escape_bytes_prefixed 当函数调用，导致写入 bytes（如向量 BLOB）时报
    "'str' object is not callable"。这里把坏符号重绑为可用的 escape_bytes。
    SQLite 环境未安装 aiomysql 时静默跳过，不影响降级路径。
    """
    try:
        import aiomysql.connection as _ac
        from pymysql.converters import escape_bytes as _escape_bytes
    except Exception:  # pragma: no cover - 未使用 aiomysql 的环境
        return
    if not callable(getattr(_ac, "escape_bytes_prefixed", None)):
        _ac.escape_bytes_prefixed = _escape_bytes
        logger.info("aiomysql_bytes_escape_patched",
                    reason="重绑 escape_bytes_prefixed → pymysql.converters.escape_bytes")


async def _probe(engine) -> bool:
    """连通性探测（带超时）：MySQL 未启动/密码错误时快速失败而非挂死。"""
    try:
        async with asyncio.timeout(5):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# 延迟创建：实际引擎由 init_database() 探测后决定（MySQL 优先，SQLite 兑底）
engine = None
session_factory: async_sessionmaker[AsyncSession] | None = None


async def ensure_engine() -> None:
    """确定最终引擎：主库探测失败 → 降级 SQLite（若配置允许）。"""
    global engine, session_factory
    primary = _create_engine(_settings.db.url)
    if await _probe(primary):
        engine = primary
        logger.info("db_connected", dialect=_settings.db.url.split("://")[0])
    elif _settings.db.fallback_sqlite:
        await primary.dispose()
        _ensure_sqlite_dir(_settings.db.sqlite_url)
        engine = _create_engine(_settings.db.sqlite_url)
        logger.warning("db_fallback_sqlite", reason="MySQL 不可达，已降级为本地 SQLite，"
                                                "仅限开发环境，数据与生产库不互通")
    else:
        await primary.dispose()
        raise ConnectionError(f"数据库不可达且未允许降级: {_settings.db.url}")
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """事务边界：正常提交、异常回滚。Service 层唯一合法的写入口。"""
    assert session_factory is not None, "引擎未初始化，请先在 lifespan 中调用 init_database()"
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入版本（只读接口使用，不自动 commit）。"""
    assert session_factory is not None, "引擎未初始化"
    async with session_factory() as session:
        yield session


# 存量库轻量迁移：create_all 只建缺失的表，不会给已有表补列；
# 新增的可空字段在此登记，启动时幂等 ALTER（MySQL/SQLite 均支持 ADD COLUMN）。
_EXTRA_COLUMNS: dict[str, dict[str, str]] = {
    "user_info": {"motto": "VARCHAR(255)"},
    "admin_info": {"avatar": "VARCHAR(512)", "motto": "VARCHAR(255)"},
    # 模型用途：存量模型默认按对话模型（chat）处理
    "llm_model": {"purpose": "VARCHAR(32) NOT NULL DEFAULT 'chat'"},
}


def _add_extra_columns(sync_conn) -> None:
    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())
    for table, columns in _EXTRA_COLUMNS.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl in columns.items():
            if name not in existing:
                sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                logger.info("column_added", table=table, column=name)


# 存量库列类型升级：create_all 不会修改已有列类型。MySQL 的 TEXT 上限 64KB，
# 长文档父分段整段落库会触发 (1406, Data too long)，故启动时幂等升级为 LONGTEXT。
# 仅 MySQL 需要；SQLite 的 TEXT 无长度限制。
_COLUMN_TYPE_UPGRADES: dict[str, dict[str, str]] = {
    "knowledge_segment": {"text": "LONGTEXT NOT NULL"},
}


def _upgrade_column_types(sync_conn) -> None:
    if sync_conn.dialect.name != "mysql":
        return
    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())
    for table, columns in _COLUMN_TYPE_UPGRADES.items():
        if table not in tables:
            continue
        for col in inspector.get_columns(table):
            ddl = columns.get(col["name"])
            if not ddl:
                continue
            if str(col["type"]).upper().startswith(ddl.split()[0].upper()):
                continue   # 已是目标类型，跳过，避免每次启动锁表
            sync_conn.execute(text(f"ALTER TABLE {table} MODIFY COLUMN {col['name']} {ddl}"))
            logger.info("column_type_upgraded", table=table, column=col["name"], ddl=ddl)


async def init_database() -> None:
    """启动时调用：探测引擎 → 建表 → 存量表补列/升级列类型（开发态；生产以 sql/schema.sql 手动初始化为准）。"""
    from app import models  # noqa: F401  确保实体已注册到 Base.metadata

    _patch_aiomysql_bytes_escape()
    await ensure_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_extra_columns)
        await conn.run_sync(_upgrade_column_types)
    logger.info("database_ready")
