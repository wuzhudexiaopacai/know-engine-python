"""领域事件总线（Redis Stream 优先 + 进程内降级）。

文档处理全程事件驱动：
    DocumentConverted  → 触发切片
    DocumentChunked    → 触发向量化
Controller/Service 只发布事件，监听器异步消费，完全解耦。

多实例部署：
- Redis 可用时事件写入 Stream（ke:events），各实例以同一消费组读取，
  一条事件只会被一个实例处理；处理成功才 ACK
- 处理失败不 ACK，消息留在 pending，由定时 XAUTOCLAIM 超时重投（至少一次）；
  超过最大投递次数后 ACK 并告警，由定时补偿任务按状态兜底重试
- Redis 未配置或 EVENT__STREAM_ENABLED=false 时退回进程内 asyncio 分发（单机语义）
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import socket
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from app.config.settings import get_settings
from app.core.logging import get_logger

logger = get_logger("app.event")

# 监听器类型：接收事件对象，可为同步或异步
Listener = Callable[[Any], Awaitable[None] | None]

_STREAM_KEY = "ke:events"
_GROUP = "ke-workers"
_MAX_LEN = 10000              # 约十万条级别的事件积压上限，防止 Stream 无界增长
_BLOCK_MS = 1000
_CLAIM_INTERVAL_SECONDS = 60  # 每分钟巡检一次未 ACK 的消息
_CLAIM_MIN_IDLE_MS = 60_000   # 处理超过 60s 未 ACK 视为实例异常，允许抢占
_MAX_DELIVERIES = 5           # 同一消息最多重投次数，超过则丢弃并交给补偿任务


@dataclass
class DomainEvent:
    """事件基类。"""

    # kw_only：作为基类元数据字段，避免子类的无默认值字段（doc_id 等）
    # 因“非默认字段跟随默认字段”而报错；构造事件时按关键字传入。
    occurred_at: datetime = field(default_factory=datetime.now, kw_only=True)


class EventBus:
    def __init__(self):
        self._listeners: dict[type, list[Listener]] = defaultdict(list)
        self._types: dict[str, type] = {}
        self._tasks: set[asyncio.Task] = set()   # 强引用池，防任务被 GC
        self._consumer: asyncio.Task | None = None
        self._claimer: asyncio.Task | None = None
        self._stopping = False
        self._redis = None
        self._stream_ready = False   # 消费组就绪后才入队，避免无消费者的场景丢事件
        self._consumer_name = f"{socket.gethostname()}-{os.getpid()}"

    # ---------------- 注册 ----------------
    def subscribe(self, event_type: type, listener: Listener) -> None:
        """注册监听器（启动时由 wiring 模块统一声明）。"""
        self._listeners[event_type].append(listener)
        self._types[event_type.__name__] = event_type

    # ---------------- 发布 ----------------
    def publish(self, event: DomainEvent) -> None:
        """发布事件：Redis 可用时入 Stream，否则进程内分发（均为非阻塞）。"""
        client = self._get_redis() if self._stream_ready else None
        if client is not None:
            task = asyncio.create_task(self._xadd(event),
                                       name=f"evt-{type(event).__name__}")
            self._track(task)
        else:
            self._dispatch_local(event)

    async def _xadd(self, event: DomainEvent) -> None:
        try:
            await self._redis.xadd(
                _STREAM_KEY,
                {"type": type(event).__name__, "payload": self._encode(event)},
                maxlen=_MAX_LEN, approximate=True)
        except Exception as e:
            # 入队失败不能阻断业务，退化为进程内分发（监听器幂等，重复执行无副作用）
            logger.warning("event_stream_publish_failed", event=type(event).__name__,
                           error=str(e), fallback="in-process")
            self._dispatch_local(event)

    def _dispatch_local(self, event: DomainEvent) -> None:
        for listener in self._collect(event):
            task = asyncio.create_task(self._invoke(listener, event),
                                       name=f"evt-{type(event).__name__}")
            self._track(task)

    def _collect(self, event: Any) -> list[Listener]:
        """支持父类订阅：监听 DomainEvent 可收到所有事件。"""
        result: list[Listener] = []
        for cls in type(event).__mro__:
            result.extend(self._listeners.get(cls, []))
        return result

    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _invoke(self, listener: Listener, event: DomainEvent) -> bool:
        """执行单个监听器；异常隔离（记日志后返回 False，由 Stream/补偿兜底）。"""
        try:
            ret = listener(event)
            if asyncio.iscoroutine(ret):
                await ret
            return True
        except Exception as e:
            logger.error("event_listener_failed", event=type(event).__name__, error=str(e))
            return False

    # ---------------- Redis Stream 消费 ----------------
    def _get_redis(self):
        """惰性创建 Redis 客户端；未配置/关闭开关/初始化失败时返回 None。"""
        cfg = get_settings()
        if not cfg.redis.url or not cfg.event.stream_enabled:
            return None
        if self._redis is None:
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(cfg.redis.url, decode_responses=True,
                                                socket_connect_timeout=1.0, socket_timeout=2.0)
            except Exception as e:  # pragma: no cover
                logger.warning("event_stream_init_failed", error=str(e), fallback="in-process")
                return None
        return self._redis

    async def start(self) -> None:
        """应用启动：Redis 可用时创建消费组并开始消费（幂等）。"""
        client = self._get_redis()
        if client is None:
            logger.info("event_bus_local", reason="redis 未配置或已关闭 Stream")
            return
        self._stopping = False
        try:
            await client.xgroup_create(_STREAM_KEY, _GROUP, id="$", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                logger.warning("event_group_create_failed", error=str(e), fallback="in-process")
                self._redis = None
                return
        self._stream_ready = True
        self._consumer = asyncio.create_task(self._consume_loop(), name="evt-consumer")
        self._claimer = asyncio.create_task(self._claim_loop(), name="evt-claimer")
        logger.info("event_bus_stream", stream=_STREAM_KEY, group=_GROUP,
                    consumer=self._consumer_name)

    async def stop(self) -> None:
        """应用关闭：停止消费与巡检，未完成的消息留给其他实例/重启后重投。"""
        self._stopping = True
        self._stream_ready = False
        for task in (self._consumer, self._claimer):
            if task is not None:
                task.cancel()
        self._consumer = self._claimer = None
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        if self._redis is not None:
            try:
                await self._redis.aclose()
            finally:
                self._redis = None

    async def _consume_loop(self) -> None:
        while not self._stopping:
            try:
                resp = await self._redis.xreadgroup(
                    _GROUP, self._consumer_name, {_STREAM_KEY: ">"},
                    count=10, block=_BLOCK_MS)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("event_stream_read_failed", error=str(e))
                await asyncio.sleep(1)
                continue
            for _, messages in resp or []:
                for msg_id, fields in messages:
                    await self._handle(msg_id, fields)

    async def _handle(self, msg_id: str, fields: dict, *, claim_deliveries: int = 0) -> bool:
        """反序列化并分发；所有监听器成功才 ACK。返回是否已 ACK。"""
        name = fields.get("type", "")
        cls = self._types.get(name)
        if cls is None:
            logger.warning("event_unknown_type", type=name)
            await self._ack(msg_id)
            return True
        try:
            event = self._decode(cls, fields.get("payload", "{}"))
        except Exception as e:
            logger.warning("event_decode_failed", type=name, error=str(e))
            await self._ack(msg_id)
            return True

        results = await asyncio.gather(
            *(self._invoke(listener, event) for listener in self._collect(event)),
            return_exceptions=True)
        if any(r is False or isinstance(r, Exception) for r in results):
            if claim_deliveries >= _MAX_DELIVERIES:
                logger.error("event_dropped", type=name, message_id=msg_id,
                             deliveries=claim_deliveries)
                await self._ack(msg_id)
                return True
            return False   # 不 ACK，等待 XAUTOCLAIM 重投
        await self._ack(msg_id)
        return True

    async def _ack(self, msg_id: str) -> None:
        try:
            await self._redis.xack(_STREAM_KEY, _GROUP, msg_id)
        except Exception as e:
            logger.warning("event_ack_failed", message_id=msg_id, error=str(e))

    async def _claim_loop(self) -> None:
        """定时巡检 pending 消息：处理超时未 ACK 的由本实例接管重投。"""
        while not self._stopping:
            await asyncio.sleep(_CLAIM_INTERVAL_SECONDS)
            try:
                await self.claim_once(_CLAIM_MIN_IDLE_MS)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("event_claim_failed", error=str(e))

    async def claim_once(self, min_idle_ms: int, count: int = 10) -> int:
        """抢占并重跑超时未 ACK 的消息，返回处理数量（供巡检与测试调用）。"""
        if self._redis is None:
            return 0
        handled = 0
        start = "0-0"
        while not self._stopping:
            try:
                start, messages, _ = await self._redis.xautoclaim(
                    _STREAM_KEY, _GROUP, self._consumer_name, min_idle_ms,
                    start_id=start, count=count)
            except Exception as e:
                logger.warning("event_autoclaim_failed", error=str(e))
                return handled
            for msg_id, fields in messages:
                deliveries = await self._delivery_count(msg_id)
                handled += 1
                await self._handle(msg_id, fields, claim_deliveries=deliveries)
            if not messages or start == "0-0":
                break
        return handled

    async def _delivery_count(self, msg_id: str) -> int:
        try:
            rows = await self._redis.xpending_range(
                _STREAM_KEY, _GROUP, min=msg_id, max=msg_id, count=1)
            return int(rows[0]["times_delivered"]) if rows else 0
        except Exception:
            return 0

    # ---------------- 序列化 ----------------
    @staticmethod
    def _encode(event: DomainEvent) -> str:
        payload: dict[str, Any] = {"occurred_at": event.occurred_at.isoformat()}
        for f in dataclasses.fields(event):
            if f.name != "occurred_at":
                payload[f.name] = getattr(event, f.name)
        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def _decode(cls: type, raw: str) -> DomainEvent:
        payload = json.loads(raw)
        occurred = payload.pop("occurred_at", None)
        event = cls(**payload)
        if occurred:
            event.occurred_at = datetime.fromisoformat(occurred)
        return event


# 进程级单例
_bus = EventBus()


def get_event_bus() -> EventBus:
    return _bus
