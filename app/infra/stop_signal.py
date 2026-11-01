"""会话级流式生成停止信号注册表。

用途：前端点击"停止"时，除断开 SSE 连接外，通过 /api/chat/interrupt 置位信号，
让服务端的生成协程在 token 循环处真正退出（关闭上游 LLM 流），并把已产出的
部分答案持久化后正常收尾。

按 conversation_id 注册 asyncio.Event；单进程内存实现，多实例部署时中断请求需
负载均衡到同一实例（或后续替换为 Redis 发布，接口不变）。
"""

from __future__ import annotations

import asyncio

_signals: dict[str, asyncio.Event] = {}


def register(key: str) -> asyncio.Event:
    """流开始前注册（同键覆盖旧信号，避免上一轮残留置位误伤本轮）。"""
    event = asyncio.Event()
    _signals[key] = event
    return event


def unregister(key: str) -> None:
    _signals.pop(key, None)


def request_stop(key: str) -> bool:
    """置位停止信号。返回是否存在活跃的流（False 表示该会话当前无生成任务）。"""
    event = _signals.get(key)
    if event is None:
        return False
    event.set()
    return True
