"""统一异常体系与全局异常处理器。

红线约束：业务代码禁止裸抛 Exception / 返回魔法数字错误码，
所有可预期错误必须走本模块异常，由全局处理器统一转换为 R 响应。
"""

from __future__ import annotations

import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger("app.exception")


# ------------------------------------------------------------------
# 异常定义
# ------------------------------------------------------------------
class BizError(Exception):
    """业务异常基类：可预期、无需报警，返回给用户友好提示。"""

    code: int = 4000
    http_status: int = 200      # 业务错误统一 200 + code，便于前端 SSE/JSON 统一处理

    def __init__(self, message: str, *, code: int | None = None, http_status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status


class AuthError(BizError):
    """未登录 / 令牌过期 / 权限不足。"""

    code = 4001
    http_status = 401


class LoginError(BizError):
    """登录失败（账号/密码错误、账号停用、验证码错误）。

    刻意不使用 401：登录接口失败属于「输入不正确」，前端不应清令牌跳登录页，
    而是直接展示服务端给出的具体原因。
    """

    code = 4010


class ForbiddenError(BizError):
    code = 4003
    http_status = 403


class NotFoundError(BizError):
    code = 4004


class ParamError(BizError):
    """入参校验失败（业务层校验，Pydantic 之外的语义校验）。"""

    code = 4009


class ConcurrentError(BizError):
    """分布式锁冲突 / 乐观锁冲突 / 重复提交。"""

    code = 4009
    http_status = 409


class ExternalServiceError(BizError):
    """外部依赖（LLM/Embedding/解析服务）失败且降级后仍不可用。

    与业务异常区分：此类错误需要触发报警（日志 ERROR 级）。
    """

    code = 5002
    http_status = 200


class DependencyError(Exception):
    """外部依赖瞬时故障（供 tenacity 重试捕获的内部异常，不对客户端暴露）。"""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


# ------------------------------------------------------------------
# 全局异常处理器
# ------------------------------------------------------------------
def register_exception_handlers(app: FastAPI) -> None:
    """注册到 FastAPI，应用启动时调用。"""

    @app.exception_handler(BizError)
    async def biz_error_handler(request: Request, exc: BizError) -> JSONResponse:
        log = logger.warning if isinstance(exc, ExternalServiceError) is False else logger.error
        log("biz_error", path=request.url.path, code=exc.code, msg=exc.message)
        return JSONResponse(
            status_code=exc.http_status,
            content={"success": False, "code": exc.code, "message": exc.message, "data": None},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 只取第一条错误，避免把内部结构暴露给前端
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", [])[1:])
        msg = f"参数校验失败: {loc} {first.get('msg', '')}".strip()
        logger.warning("param_invalid", path=request.url.path, detail=str(first.get("msg")))
        return JSONResponse(
            status_code=200,
            content={"success": False, "code": ParamError.code, "message": msg, "data": None},
        )

    # 注：asyncio.CancelledError 在 Py3.8+ 继承自 BaseException（非 Exception），
    # 不能通过 @app.exception_handler 注册（Starlette 会断言失败），
    # SSE 客户端断开由流式生成器内部 try/except CancelledError 自行处理。

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        """兜底处理：未知异常一律 ERROR 级 + 堆栈，对外隐藏内部细节（防信息泄露）。"""
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        logger.error("unhandled_error", path=request.url.path, error=str(exc), traceback="".join(tb)[-2000:])
        return JSONResponse(
            status_code=500,
            content={"success": False, "code": 5000, "message": "系统繁忙，请稍后重试", "data": None},
        )
