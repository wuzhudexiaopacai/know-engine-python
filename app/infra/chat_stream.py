"""会话级流事件缓冲（供刷新后继续接收）。

设计取舍：
- 单一进程内存实现，仅保存"当前/刚结束"会话的事件序列；开发与单 worker 部署足够，
  多实例部署时应替换为 Redis Stream（保持 begin/publish/finish/follow 同一接口）。
- attach 只对仍在生成的会话回放（约 100ms 轮询新事件），已结束的流直接视为 idle，
  避免刷新后把已完成的答案重放一遍。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

# 结束后保留时长：便于客户端在网络抖动时补拉，超时自动清理，避免内存无界增长
_TTL = 300.0
_MAX_EVENTS = 20000
_POLL_SECONDS = 0.12
_HEARTBEAT = "__heartbeat__"


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


def begin(conversation_id: str) -> None:
    """一轮生成开始：重置该会话的事件序列。"""
    _streams[conversation_id] = _State()
    _prune(time.monotonic())


def publish(conversation_id: str, event: str, data: dict) -> None:
    state = _streams.get(conversation_id)
    if state is None or state.finished:
        return
    state.events.append((event, data))
    if len(state.events) > _MAX_EVENTS:
        del state.events[:len(state.events) - _MAX_EVENTS]


def finish(conversation_id: str) -> None:
    state = _streams.get(conversation_id)
    if state is None:
        return
    state.finished = True
    state.closed_at = time.monotonic()


def is_active(conversation_id: str) -> bool:
    state = _streams.get(conversation_id)
    return state is not None and not state.finished


async def follow(conversation_id: str) -> AsyncIterator[tuple[str, dict]]:
    """回放已缓冲事件并持续跟踪新事件，直到该轮流结束。

    每约 12 秒产出一次心跳事件（controller 转成 SSE 注释行，防止代理超时断连）。
    """
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


HEARTBEAT = _HEARTBEAT
