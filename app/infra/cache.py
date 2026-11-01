"""缓存基础设施：L1 进程内 TTL-LRU + L2 Redis（自动降级）。

L1 进程内 + L2 Redis，MySQL 作为最终数据源。
工程细节：
- L1 带容量上限与 LRU 淘汰，防内存膨胀
- 空值缓存（负缓存）防缓存击穿：不存在的 key 缓存空串，短 TTL
- Redis 未配置 / 连接失败 → 透明降级为 L1 长 TTL，整个系统继续可用；
  所有 Redis 异常只记 warning，绝不上抛打断业务
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Optional

import redis.asyncio as aioredis

from app.config.settings import get_settings
from app.core.logging import get_logger

logger = get_logger("app.cache")

# 空值哨兵：区分"缓存了空串"与"未缓存"
_MISS = object()


class TtlLruCache:
    """L1 缓存：进程内 TTL + LRU，协程安全（asyncio.Lock 足够，操作均无 await）。"""

    def __init__(self, max_size: int = 2048):
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._max = max_size
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any:
        """命中返回值；未命中返回全局常量 MISS。"""
        async with self._lock:
            item = self._store.get(key)
            if item is None:
                return _MISS
            value, expire_at = item
            if expire_at < time.monotonic():
                self._store.pop(key, None)
                return _MISS
            self._store.move_to_end(key)  # LRU 触碰提升
            return value

    async def set(self, key: str, value: Any, ttl: float) -> None:
        async with self._lock:
            self._store[key] = (value, time.monotonic() + ttl)
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)  # 淘汰最久未使用

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()


class CacheClient:
    """两级缓存门面：get/set 自动完成 L1→L2 查找与回填。

    用法示例：
        value = await cache.get(key)            # None 表示未命中
        await cache.get(key, loader=fn)         # miss 时回源并写缓存（含负缓存）
    """

    def __init__(self):
        self.l1 = TtlLruCache()
        self._redis: Optional[aioredis.Redis] = None
        self._l2_ttl = 30.0          # L2 默认 TTL 30 秒
        self._neg_ttl = 10.0         # 负缓存 TTL 更短，平衡防击穿与数据新鲜度
        url = get_settings().redis.url
        if url:
            try:
                self._redis = aioredis.from_url(url, decode_responses=True,
                                                socket_connect_timeout=1.0, socket_timeout=1.0)
            except Exception as e:  # pragma: no cover - 配置错误场景
                logger.warning("redis_init_failed", error=str(e), fallback="l1_only")

    # ---------------- 基础操作 ----------------
    async def get_raw(self, key: str) -> Any:
        val = await self.l1.get(key)
        if val is not _MISS:
            return val
        if self._redis is not None:
            try:
                val = await self._redis.get(key)
                if val is not None:
                    await self.l1.set(key, val, self._l2_ttl)
                    return val
            except Exception as e:
                logger.warning("redis_get_failed", key=key, error=str(e))
        return None

    async def set_raw(self, key: str, value: Any, ttl: float | None = None) -> None:
        ttl = ttl or self._l2_ttl
        await self.l1.set(key, value, ttl)
        if self._redis is not None:
            try:
                await self._redis.set(key, value if isinstance(value, str) else str(value), ex=int(ttl))
            except Exception as e:
                logger.warning("redis_set_failed", key=key, error=str(e))

    async def delete(self, key: str) -> None:
        """写穿透：删除时同步清 L1/L2（权限变更、缓存失效场景）。"""
        await self.l1.delete(key)
        if self._redis is not None:
            try:
                await self._redis.delete(key)
            except Exception as e:
                logger.warning("redis_del_failed", key=key, error=str(e))

    # ---------------- 回源模式 ----------------
    async def get(self, key: str, loader=None, ttl: float | None = None) -> Any:
        """带-loader 的缓存读取：miss 时回源，空结果写负缓存防击穿。"""
        val = await self.get_raw(key)
        if val is not None:
            return None if val == "" else val  # 负缓存命中 → 直接返回"无数据"
        if loader is None:
            return None
        result = await loader()
        if result is None or result == "":
            await self.set_raw(key, "", self._neg_ttl)   # 防击穿：短 TTL 负缓存
            return None
        await self.set_raw(key, result, ttl)
        return result


# 进程级单例
_cache_client: CacheClient | None = None


def get_cache() -> CacheClient:
    global _cache_client
    if _cache_client is None:
        _cache_client = CacheClient()
    return _cache_client
