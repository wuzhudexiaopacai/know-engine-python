"""会话级流式生成停止信号注册表。

用途：前端点击"停止"时，除断开 SSE 连接外，通过 /api/chat/interrupt 置位信号，
让服务端的生成协程在 token 循环处真正退出（关闭上游 LLM 流），并把已产出的
部分答案持久化后正常收尾。

多实例部署：中断请求可能落到任意实例，因此信号同时写入 Redis
（ke:stop:{key}，TTL 60s）；持有生成任务的实例在后台轮询该键并置位本地 asyncio.Event。
Redis 未配置时退化为进程内信号（单机语义不变）。
"""

from __future__ import annotations

import asyncio

from app.config.settings import get_settings
from app.core.logging import get_logger

logger = get_logger("app.stop_signal")

_KEY_PREFIX = "ke:stop:"
_KEY_TTL_SECONDS = 60
_POLL_SECONDS = 0.5

_signals: dict[str, asyncio.Event] = {}
_watchers: dict[str, asyncio.Task] = {}
_redis = None


def _get_redis():
    """惰性创建 Redis 客户端；未配置或初始化失败时返回 None。"""
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
            logger.warning("stop_signal_redis_init_failed", error=str(e), fallback="in-process")
    return _redis


async def _watch(key: str, event: asyncio.Event) -> None:
    """后台轮询 Redis 停止键，命中后置位本地事件并消费该键。"""
    client = _get_redis()
    if client is None:
        return
    redis_key = _KEY_PREFIX + key
    while not event.is_set():
        try:
            if await client.getdel(redis_key):
                event.set()
                return
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception as e:
            logger.warning("stop_signal_poll_failed", error=str(e))
            return
        await asyncio.sleep(_POLL_SECONDS)


def register(key: str) -> asyncio.Event:
    """流开始前注册（同键覆盖旧信号，避免上一轮残留置位误伤本轮）。"""
    unregister(key)
    event = asyncio.Event()
    _signals[key] = event
    if _get_redis() is not None:
        _watchers[key] = asyncio.create_task(_watch(key, event))
    return event


def unregister(key: str) -> None:
    _signals.pop(key, None)
    task = _watchers.pop(key, None)
    if task is not None:
        task.cancel()


async def request_stop(key: str) -> bool:
    """置位停止信号（本地 + Redis）。

    返回是否存在活跃的流：本实例有生成任务时精确；中断请求落到其他实例时，
    只能确认已写入 Redis 通知（返回 True）。
    """
    event = _signals.get(key)
    if event is not None:
        event.set()
    local = event is not None

    client = _get_redis()
    if client is None:
        return local
    try:
        await client.set(_KEY_PREFIX + key, "1", ex=_KEY_TTL_SECONDS)
        return True
    except Exception as e:
        logger.warning("stop_signal_write_failed", error=str(e))
        return local
