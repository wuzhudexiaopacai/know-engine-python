"""熔断器（Circuit Breaker）—— LLM / Embedding / Rerank / 解析服务共用。

三态模型：CLOSED（正常）→ 连续失败达阈值 → OPEN（快速失败）
        → 半开冷却期后 → HALF_OPEN（放行探测请求）→ 成功 → CLOSED

工程细节：
- 滑动窗口统计最近 N 次调用，失败率超阈值才熔断（避免单次抖动误熔断）
- 每个外部依赖一个具名实例（breaker_registry），互不影响
- OPEN 期间 call() 直接抛 CircuitOpenError，由调用方决定降级路径
"""

from __future__ import annotations

import asyncio
import enum
import time
from collections import deque
from typing import Any, Awaitable, Callable

from app.core.logging import get_logger

logger = get_logger("app.breaker")


class CircuitOpenError(Exception):
    """熔断器打开，调用被快速拒绝。"""


class State(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, name: str, *, failure_threshold: int = 5,
                 recovery_seconds: float = 30.0, window_size: int = 20,
                 half_open_max: int = 2):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.window_size = window_size
        self.half_open_max = half_open_max

        self._state = State.CLOSED
        self._failures: deque[bool] = deque(maxlen=window_size)
        self._opened_at = 0.0
        self._half_open_successes = 0
        self._half_open_probes = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> State:
        return self._state

    async def call(self, fn: Callable[..., Awaitable[Any]], *args,
                   skip_failure_on: tuple[type[BaseException], ...] = (),
                   **kwargs) -> Any:
        """经过熔断器执行异步调用；失败按窗口统计，打开期间快速失败。

        skip_failure_on：调用方明确「不是依赖故障」的错误（如模型能力不匹配、
        请求参数错误），只上抛不计入失败窗口，避免把配置问题误判成服务不可用。
        """
        await self._before_call()
        try:
            result = await fn(*args, **kwargs)
        except skip_failure_on:
            raise
        except Exception:
            await self._on_failure()
            raise
        await self._on_success()
        return result

    async def _before_call(self) -> None:
        async with self._lock:
            if self._state == State.OPEN:
                if time.monotonic() - self._opened_at >= self.recovery_seconds:
                    # 进入半开：放行有限探测
                    self._state = State.HALF_OPEN
                    self._half_open_probes = 0
                    self._half_open_successes = 0
                    logger.info("breaker_half_open", name=self.name)
                else:
                    raise CircuitOpenError(f"依赖 [{self.name}] 已熔断，请稍后重试")
            if self._state == State.HALF_OPEN:
                if self._half_open_probes >= self.half_open_max:
                    raise CircuitOpenError(f"依赖 [{self.name}] 半开探测已达上限")
                self._half_open_probes += 1

    async def _on_success(self) -> None:
        async with self._lock:
            self._failures.append(False)
            if self._state == State.HALF_OPEN:
                self._half_open_successes += 1
                if self._half_open_successes >= self.half_open_max:
                    self._state = State.CLOSED
                    self._failures.clear()
                    logger.info("breaker_closed", name=self.name)

    async def _on_failure(self) -> None:
        async with self._lock:
            self._failures.append(True)
            if self._state == State.HALF_OPEN:
                # 探测失败 → 重新打开，重置冷却计时
                self._state = State.OPEN
                self._opened_at = time.monotonic()
                logger.warning("breaker_reopened", name=self.name)
                return
            recent_fail = sum(1 for x in self._failures if x)
            if self._state == State.CLOSED and recent_fail >= self.failure_threshold:
                self._state = State.OPEN
                self._opened_at = time.monotonic()
                logger.error("breaker_opened", name=self.name, failures=recent_fail)


# ---------------- 注册表 ----------------
_registry: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, **kwargs) -> CircuitBreaker:
    """按依赖名获取共享熔断器实例。"""
    if name not in _registry:
        _registry[name] = CircuitBreaker(name, **kwargs)
    return _registry[name]
