"""统一响应包装 R。

企业规范：所有 REST 接口必须返回 R 结构，
    { "success": bool, "code": int, "message": str, "data": T }
SSE 流式接口除外（走事件协议）。
"""

from __future__ import annotations

from typing import Generic, Optional, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class R(BaseModel, Generic[T]):
    """通用响应体。"""

    success: bool = True
    code: int = 0
    message: str = "ok"
    data: Optional[T] = None

    @classmethod
    def ok(cls, data: T | None = None, message: str = "ok") -> "R[T]":
        return cls(success=True, code=0, message=message, data=data)

    @classmethod
    def fail(cls, code: int, message: str) -> "R":
        return cls(success=False, code=code, message=message, data=None)


class PageResult(BaseModel, Generic[T]):
    """分页结果包装。"""

    total: int
    page_num: int
    page_size: int
    records: list[T]
