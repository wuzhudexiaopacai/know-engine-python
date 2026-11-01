"""雪花 ID 生成器。

64 位结构：1 符号位 + 41 毫秒时间戳 + 10 工作ID + 12 序列号。
工程细节：
- 同毫秒内序列溢出 → 等待下一毫秒
- 时钟回拨 → 使用"延后到上次时钟"策略（small backward ≤ 5ms 自旋等待，
  大于阈值直接抛错，防止生成重复 ID）
- 线程安全：threading.Lock（生成动作在纳秒级，锁竞争可忽略）
"""

from __future__ import annotations

import threading
import time

from app.core.exceptions import BizError


class SnowflakeIdGenerator:
    # 起始纪元：2024-01-01（比进程生命周期更长的固定点）
    EPOCH = 1704038400000

    WORKER_ID_BITS = 10
    SEQUENCE_BITS = 12

    MAX_WORKER_ID = (1 << WORKER_ID_BITS) - 1
    MAX_SEQUENCE = (1 << SEQUENCE_BITS) - 1

    WORKER_ID_SHIFT = SEQUENCE_BITS
    TIMESTAMP_SHIFT = SEQUENCE_BITS + WORKER_ID_BITS

    # 可容忍的最大时钟回拨（毫秒）
    MAX_BACKWARD_MS = 5

    def __init__(self, worker_id: int = 1):
        if not 0 <= worker_id <= self.MAX_WORKER_ID:
            raise ValueError(f"worker_id 必须在 0~{self.MAX_WORKER_ID} 之间")
        self.worker_id = worker_id
        self._sequence = 0
        self._last_ts = -1
        self._lock = threading.Lock()

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def next_id(self) -> int:
        """生成全局唯一、趋势递增的 64 位 ID。"""
        with self._lock:
            ts = self._now_ms()
            if ts < self._last_ts:
                # 时钟回拨处理：小幅回拨自旋等待追平，大幅回拨拒绝服务
                if self._last_ts - ts <= self.MAX_BACKWARD_MS:
                    while ts < self._last_ts:
                        time.sleep(0.001)
                        ts = self._now_ms()
                else:
                    raise BizError("检测到时钟回拨，拒绝生成ID以保证全局唯一性")
            if ts == self._last_ts:
                self._sequence = (self._sequence + 1) & self.MAX_SEQUENCE
                if self._sequence == 0:
                    # 序列溢出：等待下一毫秒
                    while ts <= self._last_ts:
                        ts = self._now_ms()
            else:
                self._sequence = 0
            self._last_ts = ts
            return ((ts - self.EPOCH) << self.TIMESTAMP_SHIFT) | \
                   (self.worker_id << self.WORKER_ID_SHIFT) | self._sequence


# 进程级单例
_snowflake = SnowflakeIdGenerator(worker_id=1)


def next_id() -> int:
    """模块级快捷函数：业务代码统一从这里取雪花 ID。"""
    return _snowflake.next_id()


def next_id_str() -> str:
    """字符串形态 ID（用于 chunk_id / message_id 等 VARCHAR 字段）。"""
    return str(_snowflake.next_id())
