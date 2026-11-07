"""请求上下文与访问日志。

职责：
1. 生成 / 透传 `X-Request-ID`，并绑定到 structlog contextvars，
   后续所有业务日志自动带上同一个 request_id（跨日志文件与 ES 可串联）；
2. 输出统一 access 日志（method / path / status / duration_ms / client_ip）；
3. 对写操作自动落一条操作审计（DB `operation_log`），显式审计优先，避免重复。

租户 / 账号身份由鉴权依赖解析后通过 `bind_identity()` 写入，
因此 access 日志与操作审计天然带上 tenant_id / account_id。
"""

from __future__ import annotations

import re
import time
import uuid
from contextvars import ContextVar
from typing import Any

import structlog
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.core.logging import get_logger

logger = get_logger("app.access")

_request_id_var: ContextVar[str] = ContextVar("request_id", default="")
_client_ip_var: ContextVar[str] = ContextVar("client_ip", default="")
_identity_var: ContextVar[dict[str, Any]] = ContextVar("audit_identity", default={})
_explicit_audited_var: ContextVar[bool] = ContextVar("explicit_audited", default=False)

_RID_ALLOWED = re.compile(r"[^A-Za-z0-9_.:-]")
_MAX_RID = 64

# 不自动记录操作审计的路径：SSE 对话、分片上传（高频）、静态资源与健康检查
_AUDIT_EXCLUDE_PREFIXES = (
    "/api/chat",
    "/api/documents/upload/chunk",
    "/api/auth/captcha",
    "/api/files",
    "/api/health",
    "/api/docs",
    "/api/openapi.json",
)
_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def current_request_id() -> str:
    return _request_id_var.get()


def current_client_ip() -> str:
    return _client_ip_var.get()


def current_identity() -> dict[str, Any]:
    return dict(_identity_var.get())


def bind_identity(tenant_id: int | None, account_id: str | None,
                  identity: str = "", name: str = "") -> None:
    """鉴权依赖解析身份后调用：绑定日志与审计所需的身份字段。"""
    data = {"tenant_id": tenant_id, "account_id": account_id,
            "identity": identity, "account_name": name}
    _identity_var.set(data)
    structlog.contextvars.bind_contextvars(
        tenant_id=tenant_id, account_id=account_id,
        identity=identity or None, account_name=name or None,
    )


def mark_explicit_audited() -> None:
    """显式审计已记录：中间件不再重复写一条通用操作日志。"""
    _explicit_audited_var.set(True)


def _extract_request_id(request: Request) -> str:
    raw = request.headers.get("X-Request-ID") or request.headers.get("X-Request-Id")
    if raw:
        cleaned = _RID_ALLOWED.sub("", raw)[:_MAX_RID]
        if cleaned:
            return cleaned
    return uuid.uuid4().hex[:20]


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _module_of(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "api":
        return parts[1].replace("-", "_")
    return "app"


def _action_of(method: str, path: str) -> str:
    parts = [p for p in path.split("/") if p]
    last = ""
    for seg in reversed(parts):
        if seg == "api" or seg.isdigit() or len(seg) > 40:
            continue
        last = seg.replace("-", "_")
        break
    return f"{method.lower()}:{last}" if last else method.lower()


def _should_audit(method: str, path: str) -> bool:
    if method.upper() not in _AUDIT_METHODS:
        return False
    if not path.startswith("/api/"):
        return False
    return not any(path.startswith(p) for p in _AUDIT_EXCLUDE_PREFIXES)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """请求级 contextvar 绑定 + access 日志 + 写操作自动审计。"""

    async def dispatch(self, request: Request,
                       call_next: RequestResponseEndpoint) -> Response:
        rid = _extract_request_id(request)
        ip = _client_ip(request)
        path = request.url.path

        _request_id_var.set(rid)
        _client_ip_var.set(ip)
        _identity_var.set({})
        _explicit_audited_var.set(False)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=rid, method=request.method, path=path, client_ip=ip,
        )

        start = time.perf_counter()
        status_code = 500
        failed = False
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        except Exception:
            failed = True
            logger.exception("request_unhandled", method=request.method, path=path)
            raise
        finally:
            duration_ms = int((time.perf_counter() - start) * 1000)
            route = getattr(request.scope.get("route"), "path", path)
            level_log = logger.warning if (failed or status_code >= 500) else logger.info
            level_log("access", status_code=status_code, duration_ms=duration_ms,
                      route=route, user_agent=(request.headers.get("user-agent") or "")[:200])

            if not failed and _should_audit(request.method, path):
                ident = _identity_var.get()
                # 匿名写操作不落审计（登录等关键操作由业务层显式记录）
                if ident.get("account_id") and not _explicit_audited_var.get():
                    try:
                        from app.modules.audit import service as audit_service
                        await audit_service.record(
                            module=_module_of(path),
                            action=_action_of(request.method, path),
                            status="SUCCESS" if status_code < 400 else "FAILED",
                            status_code=status_code,
                            request_id=rid,
                            ip=ip,
                            method=request.method,
                            path=path,
                            user_agent=(request.headers.get("user-agent") or "")[:255],
                            duration_ms=duration_ms,
                        )
                    except Exception:         # 审计失败绝不影响业务响应
                        logger.warning("audit_record_failed", path=path)

            structlog.contextvars.clear_contextvars()
