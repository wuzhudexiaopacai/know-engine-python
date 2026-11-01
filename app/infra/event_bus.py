"""进程内异步事件总线。

文档处理全程事件驱动：
    DocumentConverted  → 触发切片
    DocumentChunked    → 触发向量化
Controller/Service 只发布事件，监听器异步消费，完全解耦。

工程细节：
- 监听器异常隔离：单个监听器失败只记日志，不影响其他监听器与发布方；
  失败后的状态推进由"定时补偿任务"兜底（异步优先、定时兜底的最终一致性）
- 监听任务持有强引用（asyncio 任务弱引用会被 GC 静默取消，经典坑）
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from app.core.logging import get_logger

logger = get_logger("app.event")

# 监听器类型：接收事件对象，可为同步或异步
Listener = Callable[[Any], Awaitable[None] | None]


@dataclass
class DomainEvent:
    """事件基类。"""

    # kw_only：作为基类元数据字段，避免子类的无默认值字段（doc_id 等）
    # 因“非默认字段跟随默认字段”而报错；构造事件时按关键字传入。
    occurred_at: datetime = field(default_factory=datetime.now, kw_only=True)


class EventBus:
    def __init__(self):
        self._listeners: dict[type, list[Listener]] = defaultdict(list)
        self._tasks: set[asyncio.Task] = set()   # 强引用池，防任务被 GC

    def subscribe(self, event_type: type, listener: Listener) -> None:
        """注册监听器（启动时由 wiring 模块统一声明）。"""
        self._listeners[event_type].append(listener)

    def publish(self, event: DomainEvent) -> None:
        """非阻塞发布：为每个监听器 fork 独立 asyncio 任务。"""
        listeners = self._collect(event)
        for listener in listeners:
            task = asyncio.create_task(self._safe_invoke(listener, event),
                                       name=f"evt-{type(event).__name__}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _collect(self, event: Any) -> list[Listener]:
        """支持父类订阅：监听 DomainEvent 可收到所有事件。"""
        result: list[Listener] = []
        for cls in type(event).__mro__:
            result.extend(self._listeners.get(cls, []))
        return result

    async def _safe_invoke(self, listener: Listener, event: DomainEvent) -> None:
        try:
            ret = listener(event)
            if asyncio.iscoroutine(ret):
                await ret
        except Exception as e:
            # 异常隔离：错误落日志，等待补偿任务重试，绝不上抛
            logger.error("event_listener_failed", event=type(event).__name__, error=str(e))


# 进程级单例
_bus = EventBus()


def get_event_bus() -> EventBus:
    return _bus
