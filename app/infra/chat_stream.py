"""会话级流事件缓冲（供刷新后继续接收）。

多实例部署：事件写入 Redis List（ke:chat:stream:{conv}，TTL 5 分钟），
刷新后的 /stream/attach 可能落到任意实例，都能回放并继续跟踪同一轮生成。
Redis 未配置时退化为进程内缓冲（单机语义不变）。

设计取舍：
- 只保存"当前/刚结束"会话的事件序列，结束后保留 5 分钟便于网络抖动时补拉
- attach 只对仍在生成的会话回放（约 120ms 轮询新事件），已结束的流直接视为 idle，
  避免刷新后把已完成的答案重放一遍
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

from app.config.settings import get_settings
from app.core.logging import get_logger

logger = get_logger("app.chat_stream")

# 结束后保留时长：便于客户端在网络抖动时补拉，超时自动清理，避免存储无界增长
_TTL = 300.0
_MAX_EVENTS = 20000
_POLL_SECONDS = 0.12
_HEARTBEAT = "__heartbeat__"

_PREFIX = "ke:chat:stream:"
_TTL_SECONDS = int(_TTL)


# ---------------- 进程内降级实现 ----------------
@dataclass
class _State:
    events: list[tuple[str, dict]] = field(default_factory=list)
    finished: bool = False
    closed_at: float = 0.0


_streams: dict[str, _State] = {}


def _prune(now: float) -> None:
    stale = [k for k, s in _streams.items()
             if s.finished and now - s.closed_at > _TTL]
    for k in stale:
        _streams.pop(k, None)


# ---------------- Redis 共享实现 ----------------
_redis = None


def _get_redis():
    """惰性创建 Redis 客户端；未配置或初始化失败时返回 None（退化进程内）。"""
    global _redis
    url = get_settings().redis.url
    if not url:
        return None
    if _redis is None:
        try:
            import redis.asyncio as aioredis
            _redis = aioredis.from_url(url, decode_responses=True,
                                       socket_connect_timeout=1.0, socket_timeout=1.0)
        except Exception as e:  # pragma: no cover
            logger.warning("chat_stream_redis_init_failed", error=str(e), fallback="in-process")
            return None
    return _redis


def _keys(conversation_id: str) -> tuple[str, str]:
    base = _PREFIX + conversation_id
    return base, base + ":done"


async def begin(conversation_id: str) -> None:
    """一轮生成开始：重置该会话的事件序列。"""
    client = _get_redis()
    if client is not None:
        try:
            key, done = _keys(conversation_id)
            await client.delete(key, done)
            return
        except Exception as e:
            logger.warning("chat_stream_begin_failed", error=str(e), fallback="in-process")
    _streams[conversation_id] = _State()
    _prune(time.monotonic())


async def publish(conversation_id: str, event: str, data: dict) -> None:
    client = _get_redis()
    if client is not None:
        try:
            key, _ = _keys(conversation_id)
            await client.rpush(key, json.dumps({"e": event, "d": data}, ensure_ascii=False,
                                               separators=(",", ":")))
            await client.ltrim(key, -_MAX_EVENTS, -1)
            await client.expire(key, _TTL_SECONDS)
            return
        except Exception as e:
            # Redis 故障不能阻断生成链路，退化为进程内缓冲
            logger.warning("chat_stream_publish_failed", error=str(e), fallback="in-process")
    state = _streams.get(conversation_id)
    if state is None or state.finished:
        return
    state.events.append((event, data))
    if len(state.events) > _MAX_EVENTS:
        del state.events[:len(state.events) - _MAX_EVENTS]


async def finish(conversation_id: str) -> None:
    client = _get_redis()
    if client is not None:
        try:
            key, done = _keys(conversation_id)
            await client.set(done, "1", ex=_TTL_SECONDS)
            await client.expire(key, _TTL_SECONDS)
            return
        except Exception as e:
            logger.warning("chat_stream_finish_failed", error=str(e), fallback="in-process")
    state = _streams.get(conversation_id)
    if state is None:
        return
    state.finished = True
    state.closed_at = time.monotonic()


async def is_active(conversation_id: str) -> bool:
    client = _get_redis()
    if client is not None:
        try:
            key, done = _keys(conversation_id)
            if not await client.exists(done):
                return bool(await client.exists(key))
        except Exception:
            pass
        return False
    state = _streams.get(conversation_id)
    return state is not None and not state.finished


async def _follow_redis(conversation_id: str) -> AsyncIterator[tuple[str, dict]]:
    client = _get_redis()
    key, done = _keys(conversation_id)
    index = 0
    idle_ticks = 0
    while True:
        try:
            raw = await client.lrange(key, index, -1)
            finished = bool(await client.exists(done))
        except Exception as e:
            logger.warning("chat_stream_follow_failed", error=str(e))
            return
        for item in raw:
            index += 1
            try:
                payload = json.loads(item)
                yield payload["e"], payload["d"]
            except (TypeError, ValueError, KeyError):
                continue
        idle_ticks = 0 if raw else idle_ticks + 1
        if finished and not raw:
            return
        if not finished and not raw and not await client.exists(key):
            return   # 会话已被清理或从未开始
        await asyncio.sleep(_POLL_SECONDS)
        if idle_ticks and idle_ticks % 100 == 0:
            yield _HEARTBEAT, {}


async def _follow_local(conversation_id: str) -> AsyncIterator[tuple[str, dict]]:
    index = 0
    idle_ticks = 0
    while True:
        state = _streams.get(conversation_id)
        if state is None:
            return
        while index < len(state.events):
            event, data = state.events[index]
            index += 1
            idle_ticks = 0
            yield event, data
        if state.finished:
            return
        await asyncio.sleep(_POLL_SECONDS)
        idle_ticks += 1
        if idle_ticks and idle_ticks % 100 == 0:
            yield _HEARTBEAT, {}


async def follow(conversation_id: str) -> AsyncIterator[tuple[str, dict]]:
    """回放已缓冲事件并持续跟踪新事件，直到该轮流结束。

    每约 12 秒产出一次心跳事件（controller 转成 SSE 注释行，防止代理超时断连）。
    """
    if _get_redis() is not None:
        async for item in _follow_redis(conversation_id):
            yield item
    else:
        async for item in _follow_local(conversation_id):
            yield item


HEARTBEAT = _HEARTBEAT
