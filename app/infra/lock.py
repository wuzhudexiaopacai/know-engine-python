"""分布式锁（Redis 优先 + 进程内降级）。

工程细节：
- Redis 可用：SET NX PX + Lua 原子释放（比对持有者标识，防误删他人锁）
- Redis 不可用：自动降级为进程内 asyncio.Lock —— 单机部署语义等价
- wait_time=0 语义：锁被占用立即抛 ConcurrentError（切片/向量化的幂等保护）
- 看门狗：持有期间后台任务定期续期，防业务超时后锁提前失效
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from app.config.settings import get_settings
from app.core.exceptions import ConcurrentError
from app.core.logging import get_logger

logger = get_logger("app.lock")

# 释放脚本：仅当持有者匹配才删除（防止误删他人锁）
_LUA_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# 续期脚本：仅当持有者匹配才延长租约（看门狗用）
_LUA_RENEW = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
"""


class _LocalLockRegistry:
    """进程内锁注册表：key → asyncio.Lock，空闲自动回收防内存泄漏。"""

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}
        self._guard = asyncio.Lock()

    async def acquire(self, key: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.setdefault(key, asyncio.Lock())
            self._refs[key] = self._refs.get(key, 0) + 1
            return lock

    async def release(self, key: str) -> None:
        async with self._guard:
            self._refs[key] = self._refs.get(key, 1) - 1
            if self._refs[key] <= 0:
                self._locks.pop(key, None)
                self._refs.pop(key, None)


class DistributedLock:
    """双模锁：Redis 优先，进程内降级。使用 `async with lock.hold(scene, key):`。"""

    def __init__(self, default_lease_ms: int = 30_000):
        self._lease_ms = default_lease_ms
        self._local = _LocalLockRegistry()
        self._redis = None
        # 每把锁独立的持有者标识与续期任务（按 lock_key 隔离，防并发持锁互相覆盖）
        self._tokens: dict[str, str] = {}
        self._renewals: dict[str, asyncio.Task] = {}
        url = get_settings().redis.url
        if url:
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(url, decode_responses=True,
                                                socket_connect_timeout=1.0, socket_timeout=1.0)
            except Exception as e:  # pragma: no cover
                logger.warning("lock_redis_init_failed", error=str(e), fallback="in-process")

    def _redis_available(self) -> bool:
        return self._redis is not None

    @asynccontextmanager
    async def hold(self, scene: str, key: str, *, wait_time: float = 0.0,
                   lease_ms: int | None = None) -> AsyncIterator[None]:
        """获取锁并在退出时释放。

        wait_time: 最长等待秒数；0 表示不等待，占用即抛 ConcurrentError
        """
        lease_ms = lease_ms or self._lease_ms
        lock_key = f"ke:lock:{scene}:{key}"

        if self._redis_available():
            token = await self._acquire_redis(lock_key, wait_time, lease_ms)
            if token is None:
                raise ConcurrentError(f"操作正在处理中，请勿重复提交（{scene}）")
            if token:  # 空串表示 Redis 故障放行，无需看门狗
                self._renewals[lock_key] = asyncio.create_task(
                    self._renew_loop(lock_key, token, lease_ms),
                    name=f"lock-renew-{scene}-{key}")
            try:
                yield
            finally:
                await self._release_redis(lock_key)
        else:
            local_key = lock_key
            lock = await self._local.acquire(local_key)
            try:
                if wait_time <= 0:
                    if lock.locked():
                        raise ConcurrentError(f"操作正在处理中，请勿重复提交（{scene}）")
                    await lock.acquire()
                else:
                    try:
                        await asyncio.wait_for(lock.acquire(), timeout=wait_time)
                    except asyncio.TimeoutError:
                        raise ConcurrentError(f"等待锁超时（{scene}）") from None
                yield
            finally:
                lock.release()
                await self._local.release(local_key)

    # ---------------- Redis 实现 ----------------
    async def _acquire_redis(self, lock_key: str, wait_time: float, lease_ms: int) -> str | None:
        """成功返回持有者 token；锁被占用返回 None；Redis 故障放行返回空串。"""
        token = secrets.token_hex(8)
        deadline = asyncio.get_event_loop().time() + max(wait_time, 0.001)
        while True:
            try:
                ok = await self._redis.set(lock_key, token, nx=True, px=lease_ms)
                if ok:
                    self._tokens[lock_key] = token
                    return token
            except Exception as e:
                logger.warning("redis_lock_failed", error=str(e), fallback="allow")
                return ""  # Redis 故障时放行（可用性优先，单机场景由事件流保证串行）
            if asyncio.get_event_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.05)

    async def _renew_loop(self, lock_key: str, token: str, lease_ms: int) -> None:
        """看门狗：按租约的 1/3 周期续期；锁已丢失则告警退出。"""
        interval = max(lease_ms / 3, 500) / 1000
        while True:
            await asyncio.sleep(interval)
            try:
                ok = await self._redis.eval(_LUA_RENEW, 1, lock_key, token, lease_ms)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("lock_renew_failed", key=lock_key, error=str(e))
                continue
            if not ok:
                logger.warning("lock_lost", key=lock_key,
                               hint="锁已过期或被抢占，业务可能并发执行")
                return

    async def _release_redis(self, lock_key: str) -> None:
        task = self._renewals.pop(lock_key, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        token = self._tokens.pop(lock_key, None)
        if not token:
            return
        try:
            await self._redis.eval(_LUA_RELEASE, 1, lock_key, token)
        except Exception as e:
            logger.warning("redis_unlock_failed", key=lock_key, error=str(e))


# 进程级单例
_lock: DistributedLock | None = None


def get_lock() -> DistributedLock:
    global _lock
    if _lock is None:
        _lock = DistributedLock()
    return _lock
