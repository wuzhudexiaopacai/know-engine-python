"""日志中心 REST 接口。

- 操作日志：MySQL `operation_log`（谁做了什么，结构化分页查询）；
- 系统日志：Elasticsearch 日志索引（异常堆栈、耗时、request_id 全链路检索）。
两处都需要 `log:view` 权限；租户账号只能看本租户，平台超管可跨租户。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.deps import CurrentUser, require_permission
from app.core.response import R
from app.modules.audit import service as audit_service
from app.modules.rbac.constants import PermCode

router = APIRouter(prefix="/api/admin/logs", tags=["日志中心"])


def _scope_tenant(user: CurrentUser, tenant_id: int | None) -> int | None:
    """平台超管可按参数切租户；租户账号固定为本租户。"""
    if user.is_platform_admin:
        return tenant_id
    return user.tenant_id


@router.get("/operations")
async def list_operations(
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
    keyword: str | None = None,
    module: str | None = None,
    action: str | None = None,
    status: str | None = None,
    account_id: int | None = None,
    tenant_id: int | None = Query(None),
    start: str | None = None,
    end: str | None = None,
    user: CurrentUser = Depends(require_permission(PermCode.LOG_VIEW)),
) -> R:
    return R.ok(await audit_service.list_operations(
        page=page, size=size, tenant_id=_scope_tenant(user, tenant_id),
        keyword=keyword, module=module, action=action, status=status,
        account_id=account_id, start=start, end=end,
    ))


@router.get("/operations/modules")
async def list_modules(
    user: CurrentUser = Depends(require_permission(PermCode.LOG_VIEW)),
) -> R:
    return R.ok(await audit_service.distinct_modules())


@router.get("/system")
async def list_system_logs(
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
    keyword: str | None = None,
    level: str | None = None,
    request_id: str | None = None,
    module: str | None = None,
    tenant_id: int | None = Query(None),
    start: str | None = None,
    end: str | None = None,
    user: CurrentUser = Depends(require_permission(PermCode.LOG_VIEW)),
) -> R:
    """系统日志：ES 不可用时返回 degraded，前端展示降级提示而非报错。"""
    data = await audit_service.search_system(
        page=page, size=size, tenant_id=_scope_tenant(user, tenant_id),
        keyword=keyword, level=level, request_id=request_id, module=module,
        start=start, end=end,
    )
    return R.ok(data)
