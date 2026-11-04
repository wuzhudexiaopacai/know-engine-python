"""定时补偿与治理任务（APScheduler，对应"异步优先 + 定时兜底"的最终一致性设计）。

三类任务：
1. 文档流水线补偿：按「版本」维度扫描停滞/失败的版本，按当前状态或失败阶段重发对应
   事件，复用流水线幂等防护（状态门槛 + 分布式锁）安全重试；
   重试次数上限 + 指数退避，避免坏数据无限重试把整库拖垮
2. 上传会话清理：删除超过 TTL 未完成的分片上传目录，回收磁盘
3. 问答缓存治理：过期下线、索引失败重试、长期零命中准出
"""

from __future__ import annotations

import functools
from datetime import datetime, timedelta

from sqlalchemy import select, update

from app.config.settings import get_settings
from app.core.database import session_scope
from app.core.exceptions import ConcurrentError
from app.core.logging import get_logger
from app.models.constants import CacheStatus, DisableReason, DocumentStatus
from app.models.entities import DocumentVersion, KnowledgeDocument, QaCacheEntry
from app.modules.document.events import (
    DocumentChunked, DocumentConverted, DocumentUploaded,
)

logger = get_logger("app.jobs.compensate")

# 单版本最多自动补偿次数（人工 retry 不受此限）
MAX_ATTEMPTS = 5
# 退避上限，避免坏数据被放到几小时后才重试
_MAX_BACKOFF_MINUTES = 60

# 版本状态 → 需重发的事件（事件入口自带状态门槛，天然幂等）
_EVENT_BY_STATUS = {
        DocumentStatus.UPLOADED.value: DocumentUploaded,
        DocumentStatus.CONVERTING.value: DocumentUploaded,
        DocumentStatus.CONVERTED.value: DocumentConverted,
        DocumentStatus.CHUNKING.value: DocumentConverted,
        DocumentStatus.CHUNKED.value: DocumentChunked,
}

# 失败阶段 → 从该阶段入口重跑（阶段轨迹里记录了 failedStage）
_EVENT_BY_FAILED_STAGE = {
        "convert": DocumentUploaded,
        "split": DocumentConverted,
        "embedding": DocumentChunked,
}


async def monitor_event_backlog() -> None:
    """事件队列积压巡检：超阈值打告警日志（由日志采集/外部告警订阅该事件名）。"""
    from app.infra.event_bus import get_event_bus

    stats = await get_event_bus().stats()
    if stats is None:
        return
    cfg = get_settings().event
    if stats["length"] >= cfg.backlog_warn or stats["pending"] >= cfg.pending_warn:
        logger.warning("event_backlog_high", **stats,
                       backlog_warn=cfg.backlog_warn, pending_warn=cfg.pending_warn)


async def compensate_documents(stall_minutes: int = 5) -> int:
    """扫描停滞/失败版本并按阶段重发事件。返回触发数量。

    以 DocumentVersion 为扫描单位（而非主表状态），旧版本的中间态也能被补偿，
    不会出现"文档已 VECTOR_STORED 但某版本永远卡在 CHUNKING"的孤儿版本。
    """
    from app.infra.event_bus import get_event_bus

    now = datetime.now()
    stuck_statuses = list(_EVENT_BY_STATUS) + [DocumentStatus.FAILED.value]
    pending: list[tuple[type, int, int]] = []
    async with session_scope() as session:
        rows = (await session.execute(
            select(DocumentVersion, KnowledgeDocument)
            .join(KnowledgeDocument, KnowledgeDocument.doc_id == DocumentVersion.doc_id)
            .where(
                DocumentVersion.deleted == 0,
                KnowledgeDocument.deleted == 0,
                DocumentVersion.status.in_(stuck_statuses),
                DocumentVersion.updated_at < now - timedelta(minutes=stall_minutes))
            .limit(200))).all()
        for ver, doc in rows:
            ext = dict(doc.extension or {})
            entry = dict((ext.get("versions") or {}).get(str(ver.version_id)) or {})
            attempts = int(entry.get("attempts") or 0)
            if attempts >= MAX_ATTEMPTS:
                continue
            # 指数退避：第 n 次重试前至少等待 stall_minutes * 2^n 分钟
            backoff = timedelta(minutes=min(stall_minutes * (2 ** attempts),
                                            _MAX_BACKOFF_MINUTES))
            if ver.updated_at >= now - backoff:
                continue
            event_cls = _EVENT_BY_STATUS.get(ver.status)
            if event_cls is None:
                failed_stage = entry.get("failedStage")
                event_cls = _EVENT_BY_FAILED_STAGE.get(failed_stage)
                if event_cls is None:
                    # 无失败阶段记录（例如建版本时即失败）：从转换阶段兜底重跑
                    event_cls = DocumentUploaded
            entry["attempts"] = attempts + 1
            versions = dict(ext.get("versions") or {})
            versions[str(ver.version_id)] = entry
            ext["versions"] = versions
            await session.execute(
                update(KnowledgeDocument)
                .where(KnowledgeDocument.doc_id == doc.doc_id)
                .values(extension=ext))
            pending.append((event_cls, ver.doc_id, ver.version_id))

    bus = get_event_bus()
    for event_cls, doc_id, version_id in pending:
        bus.publish(event_cls(doc_id=doc_id, version_id=version_id))
    if pending:
        logger.info("compensate_documents_triggered", count=len(pending))
    return len(pending)


async def sweep_upload_sessions() -> int:
    from app.modules.document.upload_service import get_upload_service
    return get_upload_service().sweep_expired()


async def maintain_qa_cache() -> None:
    """问答缓存治理：过期下线 + 索引重试 + 零命中准出。"""
    now = datetime.now()
    async with session_scope() as session:
        # 1) 到期自动 EXPIRED
        await session.execute(update(QaCacheEntry)
                              .where(QaCacheEntry.status == CacheStatus.ACTIVE,
                                     QaCacheEntry.expires_at.is_not(None),
                                     QaCacheEntry.expires_at < now)
                              .values(status=CacheStatus.EXPIRED, disabled_at=now,
                                      disable_reason=DisableReason.EXPIRED))
        # 2) 激活满 7 天仍零命中 → 低命中准出
        week_ago = now - timedelta(days=7)
        await session.execute(update(QaCacheEntry)
                              .where(QaCacheEntry.status == CacheStatus.ACTIVE,
                                     QaCacheEntry.activated_at.is_not(None),
                                     QaCacheEntry.activated_at < week_ago,
                                     QaCacheEntry.hits == 0)
                              .values(status=CacheStatus.DISABLED, disabled_at=now,
                                      disable_reason=DisableReason.LOW_HIT_RATE))
        # 3) 已审核通过（APPROVED）或已激活但索引缺失 → 重试补齐并激活
        broken = (await session.execute(
            select(QaCacheEntry.id).where(
                QaCacheEntry.status.in_([CacheStatus.APPROVED, CacheStatus.ACTIVE]),
                QaCacheEntry.embedding.is_(None)))).scalars().all()
    if broken:
        from app.modules.qa_cache import service as qa_cache
        for entry_id in broken:
            await qa_cache._index_embedding(entry_id, activate=True)


# ------------------------------------------------------------------
# 调度器装配
# ------------------------------------------------------------------
def _single_flight(job_name: str, lease_ms: int):
    """跨实例单活包装：同一时刻只允许一个实例执行该任务。

    Redis 可用时通过分布式锁抢占；拿不到锁说明别的实例正在跑，直接跳过。
    Redis 未配置时退化为进程内锁，各实例都会执行（单机语义不变）。
    """
    def decorator(func):
        @functools.wraps(func)
        async def runner(*args, **kwargs):
            from app.infra.lock import get_lock
            try:
                async with get_lock().hold("scheduler", job_name, wait_time=0,
                                           lease_ms=lease_ms):
                    await func(*args, **kwargs)
            except ConcurrentError:
                logger.debug("scheduler_job_skipped", job=job_name)
        return runner
    return decorator


def build_scheduler():
    """创建并配置 AsyncIOScheduler（由 main lifespan 启动/关闭）。"""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(_single_flight("compensate_documents", 5 * 60_000)(compensate_documents),
                      "interval", minutes=2,
                      id="compensate_documents", max_instances=1, coalesce=True,
                      misfire_grace_time=60)
    scheduler.add_job(_single_flight("sweep_upload_sessions", 30 * 60_000)(sweep_upload_sessions),
                      "interval", hours=6,
                      id="sweep_upload_sessions", max_instances=1, coalesce=True)
    scheduler.add_job(_single_flight("maintain_qa_cache", 30 * 60_000)(maintain_qa_cache),
                      "interval", minutes=30,
                      id="maintain_qa_cache", max_instances=1, coalesce=True)
    scheduler.add_job(_single_flight("monitor_event_backlog", 60_000)(monitor_event_backlog),
                      "interval", minutes=1,
                      id="monitor_event_backlog", max_instances=1, coalesce=True)
    return scheduler
