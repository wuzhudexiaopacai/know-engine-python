"""操作审计 Service：异步批量落库 + 日志中心查询。

写入路径（不阻塞业务）：
    业务代码 await record(...) → 入 asyncio 有界队列 → 后台消费协程批量写 MySQL；
    队列满 / 数据库异常只丢弃审计并告警，请求照常返回。

为什么审计要单独一张表而不是只写文件：
- 「谁做了什么」必须可结构化查询（按人、模块、动作、时间、request_id）；
- 文件/ES 用于排障与全文检索，两者在日志中心互补呈现。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select

from app.config.settings import get_settings
from app.core.database import session_scope
from app.core.exceptions import ParamError
from app.core.logging import get_logger
from app.core.request_context import (
    current_client_ip, current_identity, current_request_id, mark_explicit_audited,
)
from app.models.entities import OperationLog
from app.models.queries import base_count, base_filter

logger = get_logger("app.audit")

_BATCH_SIZE = 100

_queue: asyncio.Queue[dict[str, Any] | None] | None = None
_worker: asyncio.Task | None = None


def _queue_size() -> int:
    return max(1000, get_settings().logging.es_queue_size // 5)


async def start() -> None:
    """启动审计消费协程（lifespan 调用，幂等）。"""
    global _queue, _worker
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_queue_size())
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_consume(), name="ke-audit-writer")
    logger.info("audit_writer_started")


async def stop() -> None:
    """停止消费协程：先尽量清空队列，再退出。"""
    global _queue, _worker
    if _queue is None or _worker is None:
        return
    try:
        await flush()
        await _queue.put(None)
        await asyncio.wait_for(_worker, timeout=5.0)
    except Exception:                 # pragma: no cover - 关闭阶段不阻塞退出
        _worker.cancel()
    finally:
        _worker = None


async def flush() -> None:
    """把当前队列中的审计全部落库（测试与优雅关闭使用）。"""
    if _queue is None:
        return
    drained: list[dict[str, Any]] = []
    while True:
        try:
            item = _queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if item is not None:
            drained.append(item)
    for i in range(0, len(drained), _BATCH_SIZE):
        await _write_batch(drained[i:i + _BATCH_SIZE])


async def record(*, module: str, action: str, status: str = "SUCCESS",
                 status_code: int | None = None, target_type: str | None = None,
                 target_id: str | int | None = None, detail: dict | None = None,
                 account_id: int | str | None = None, identity: str | None = None,
                 account_name: str | None = None, tenant_id: int | None = None,
                 request_id: str | None = None, ip: str | None = None,
                 method: str | None = None, path: str | None = None,
                 user_agent: str | None = None, duration_ms: int | None = None,
                 explicit: bool = True) -> None:
    """记录一条操作审计；explicit=True 表示业务层语义记录（中间件不再重复记）。"""
    if explicit:
        mark_explicit_audited()
    ident = current_identity()
    raw_account = account_id if account_id not in (None, "") else ident.get("account_id")
    try:
        account_value = int(raw_account) if raw_account not in (None, "") else None
    except (TypeError, ValueError):
        account_value = None
    entry: dict[str, Any] = {
        "module": module[:32], "action": action[:64], "status": status,
        "status_code": status_code,
        "target_type": (target_type or None) and str(target_type)[:32],
        "target_id": None if target_id is None else str(target_id)[:64],
        "detail": detail,
        "account_id": account_value,
        "identity": identity or ident.get("identity") or None,
        "account_name": account_name or ident.get("account_name") or None,
        "tenant_id": tenant_id if tenant_id is not None else ident.get("tenant_id"),
        "request_id": request_id or current_request_id() or None,
        "ip": ip or current_client_ip() or None,
        "method": method, "path": path, "user_agent": user_agent,
        "duration_ms": duration_ms,
    }
    if _queue is None:                 # 测试/脚本未启动消费者：直接落库
        await _write_batch([entry])
        return
    try:
        _queue.put_nowait(entry)
    except asyncio.QueueFull:
        logger.warning("audit_queue_full_dropped", module=module, action=action)


async def _consume() -> None:
    assert _queue is not None
    while True:
        item = await _queue.get()
        if item is None:
            return
        batch = [item]
        while len(batch) < _BATCH_SIZE:
            try:
                nxt = _queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if nxt is None:
                await _write_batch(batch)
                return
            batch.append(nxt)
        await _write_batch(batch)


async def _write_batch(entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    try:
        async with session_scope() as session:
            for e in entries:
                session.add(OperationLog(
                    tenant_id=e.get("tenant_id") or 1,
                    account_id=e.get("account_id"),
                    identity=(e.get("identity") or "")[:32] or None,
                    account_name=(e.get("account_name") or "")[:128] or None,
                    module=e["module"], action=e["action"], status=e["status"],
                    status_code=e.get("status_code"), target_type=e.get("target_type"),
                    target_id=e.get("target_id"), method=e.get("method"),
                    path=e.get("path"), request_id=e.get("request_id"),
                    ip=e.get("ip"), user_agent=(e.get("user_agent") or "")[:255] or None,
                    detail=e.get("detail"), duration_ms=e.get("duration_ms"),
                ))
    except Exception as exc:         # 审计失败绝不影响业务
        logger.warning("audit_write_failed", count=len(entries), error=str(exc)[:200])


# ------------------------------------------------------------------
# 日志中心：操作日志查询
# ------------------------------------------------------------------
def _to_vo(row: OperationLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "request_id": row.request_id,
        "tenant_id": row.tenant_id,
        "account_id": row.account_id,
        "identity": row.identity,
        "account_name": row.account_name,
        "module": row.module,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "method": row.method,
        "path": row.path,
        "status": row.status,
        "status_code": row.status_code,
        "ip": row.ip,
        "duration_ms": row.duration_ms,
        "detail": row.detail,
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds")
        if row.created_at else None,
    }


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ParamError(f"时间格式不正确，应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS：{value}")


async def list_operations(*, page: int, size: int, tenant_id: int | None = None,
                          keyword: str | None = None, module: str | None = None,
                          action: str | None = None, status: str | None = None,
                          account_id: int | None = None,
                          start: str | None = None, end: str | None = None) -> dict:
    """分页查询操作日志；租户隔离由全局钩子自动完成。"""
    start_dt, end_dt = _parse_dt(start), _parse_dt(end)
    async with session_scope() as session:
        conditions = []
        if tenant_id is not None:
            conditions.append(OperationLog.tenant_id == tenant_id)
        if keyword:
            like = f"%{keyword.strip()}%"
            conditions.append(or_(OperationLog.account_name.like(like),
                                  OperationLog.action.like(like),
                                  OperationLog.path.like(like),
                                  OperationLog.request_id.like(like)))
        if module:
            conditions.append(OperationLog.module == module)
        if action:
            conditions.append(OperationLog.action == action)
        if status:
            conditions.append(OperationLog.status == status.upper())
        if account_id is not None:
            conditions.append(OperationLog.account_id == account_id)
        if start_dt:
            conditions.append(OperationLog.created_at >= start_dt)
        if end_dt:
            conditions.append(OperationLog.created_at <= end_dt)

        total = (await session.execute(
            base_count(OperationLog).where(*conditions))).scalar_one()
        rows = (await session.execute(
            base_filter(OperationLog).where(*conditions)
            .order_by(OperationLog.created_at.desc(), OperationLog.id.desc())
            .offset((page - 1) * size).limit(size))).scalars().all()
    return {"total": total, "page_num": page, "page_size": size,
            "records": [_to_vo(r) for r in rows]}


async def distinct_modules() -> list[str]:
    """日志中心筛选下拉：当前可见范围内出现过的模块。"""
    async with session_scope() as session:
        rows = (await session.execute(
            base_filter(OperationLog).with_only_columns(OperationLog.module)
            .distinct())).scalars().all()
    return sorted({m for m in rows if m})


async def search_system(*, page: int, size: int, tenant_id: int | None = None,
                        **filters: Any) -> dict:
    """系统日志（ES）：转发到 infra.log_store，未配置时返回降级说明。"""
    from app.infra.log_store import search_system_logs

    return await search_system_logs(page=page, size=size, tenant_id=tenant_id, **filters)
