"""后台管理 REST 接口：成员账号、角色、租户与模型配置。

鉴权全部改为 RBAC 权限点：平台超管持有通配权限，按需通过 tenant_id 参数
跨租户操作；租户管理员只能操作本租户数据。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.deps import CurrentUser, require_permission, require_platform_admin
from app.core.response import R
from app.modules.admin import service as admin_service
from app.modules.admin.schemas import UserCreateRequest, UserUpdateRequest
from app.modules.model import service as model_service
from app.modules.model.schemas import ModelSaveRequest, ModelTestRequest, RoleBindRequest
from app.modules.rbac import service as rbac_service
from app.modules.rbac.constants import PermCode
from app.modules.rbac.schemas import (
    RoleAssignRequest, RoleSaveRequest, TenantCreateRequest, TenantUpdateRequest,
)

router = APIRouter(prefix="/api/admin", tags=["后台管理"])


def _scope_tenant(user: CurrentUser, tenant_id: int | None = None) -> int | None:
    """平台超管可按参数切租户；租户账号固定为本租户。"""
    if user.is_platform_admin:
        return tenant_id
    return user.tenant_id


# ------------------------------------------------------------------
# 成员账号
# ------------------------------------------------------------------
@router.get("/users")
async def list_users(page: int = Query(1, ge=1), size: int = Query(10, ge=1, le=100),
                     keyword: str | None = None, tenant_id: int | None = Query(None),
                     user: CurrentUser = Depends(require_permission(PermCode.MEMBER_MANAGE))) -> R:
    return R.ok(await admin_service.list_users(page, size, keyword, _scope_tenant(user, tenant_id)))


@router.post("/users")
async def create_user(body: UserCreateRequest,
                      user: CurrentUser = Depends(require_permission(PermCode.MEMBER_MANAGE))) -> R:
    scope = _scope_tenant(user, body.tenant_id)
    return R.ok(await admin_service.create_user(body, scope), "成员已创建")


@router.put("/users/{user_id}")
async def update_user(user_id: int, body: UserUpdateRequest,
                      lock_version: int = Query(0, ge=0),
                      user: CurrentUser = Depends(require_permission(PermCode.MEMBER_MANAGE))) -> R:
    return R.ok(await admin_service.update_user(user_id, body, lock_version,
                                                _scope_tenant(user)), "成员已更新")


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, lock_version: int = Query(0, ge=0),
                      user: CurrentUser = Depends(require_permission(PermCode.MEMBER_MANAGE))) -> R:
    await admin_service.delete_user(user_id, lock_version)
    return R.ok(None, "成员已删除")


@router.put("/users/{user_id}/roles")
async def assign_user_roles(user_id: int, body: RoleAssignRequest,
                            user: CurrentUser = Depends(require_permission(PermCode.MEMBER_MANAGE))) -> R:
    scope = _scope_tenant(user)
    if scope is None:
        from app.modules.rbac.service import load_tenant_of_account
        scope = await load_tenant_of_account(user_id)
    roles = await rbac_service.set_member_roles(user_id, body.role_ids, scope)
    return R.ok({"roles": roles}, "角色已更新")


# ------------------------------------------------------------------
# 角色与权限
# ------------------------------------------------------------------
@router.get("/roles")
async def list_roles(tenant_id: int | None = Query(None),
                     user: CurrentUser = Depends(require_permission(PermCode.ROLE_MANAGE, PermCode.MEMBER_MANAGE))) -> R:
    scope = _scope_tenant(user, tenant_id)
    if scope is None:
        return R.ok([])
    return R.ok(await rbac_service.list_roles(scope))


@router.post("/roles")
async def create_role(body: RoleSaveRequest,
                      user: CurrentUser = Depends(require_permission(PermCode.ROLE_MANAGE))) -> R:
    from app.core.exceptions import ParamError
    scope = _scope_tenant(user)
    if scope is None:
        raise ParamError("请指定目标租户后再创建角色")
    return R.ok(await rbac_service.create_role(scope, body), "角色已创建")


@router.put("/roles/{role_id}")
async def update_role(role_id: int, body: RoleSaveRequest,
                      user: CurrentUser = Depends(require_permission(PermCode.ROLE_MANAGE))) -> R:
    scope = _scope_tenant(user)
    if scope is None:
        from app.core.exceptions import ParamError
        raise ParamError("平台角色请在具体租户下维护")
    return R.ok(await rbac_service.update_role(scope, role_id, body), "角色已更新")


@router.get("/permissions")
async def list_permissions(
        user: CurrentUser = Depends(require_permission(PermCode.ROLE_MANAGE, PermCode.MEMBER_MANAGE))) -> R:
    return R.ok(await rbac_service.list_permissions())


# ------------------------------------------------------------------
# 租户（平台超管）
# ------------------------------------------------------------------
@router.get("/tenants")
async def list_tenants(page: int = Query(1, ge=1), size: int = Query(10, ge=1, le=100),
                       keyword: str | None = None,
                       user: CurrentUser = Depends(require_platform_admin)) -> R:
    return R.ok(await rbac_service.list_tenants(page, size, keyword))


@router.post("/tenants")
async def create_tenant(body: TenantCreateRequest,
                        user: CurrentUser = Depends(require_platform_admin)) -> R:
    return R.ok(await rbac_service.create_tenant(body), "租户已创建")


@router.put("/tenants/{tenant_id}")
async def update_tenant(tenant_id: int, body: TenantUpdateRequest,
                        user: CurrentUser = Depends(require_platform_admin)) -> R:
    return R.ok(await rbac_service.update_tenant(tenant_id, body), "租户已更新")


# ------------------------------------------------------------------
# 对话模型配置
# ------------------------------------------------------------------
@router.get("/models")
async def list_models(user: CurrentUser = Depends(require_permission(PermCode.MODEL_MANAGE))) -> R:
    return R.ok(await model_service.list_models())


@router.post("/models")
async def create_model(body: ModelSaveRequest,
                       user: CurrentUser = Depends(require_permission(PermCode.MODEL_MANAGE))) -> R:
    return R.ok(await model_service.create_model(body), "模型已新增")


@router.put("/models/{model_id}")
async def update_model(model_id: int, body: ModelSaveRequest,
                       lock_version: int = Query(0, ge=0),
                       user: CurrentUser = Depends(require_permission(PermCode.MODEL_MANAGE))) -> R:
    return R.ok(await model_service.update_model(model_id, body, lock_version), "模型已更新")


@router.delete("/models/{model_id}")
async def delete_model(model_id: int, lock_version: int = Query(0, ge=0),
                       user: CurrentUser = Depends(require_permission(PermCode.MODEL_MANAGE))) -> R:
    await model_service.delete_model(model_id, lock_version)
    return R.ok(None, "模型已删除")


@router.post("/models/test")
async def test_model(body: ModelTestRequest,
                     user: CurrentUser = Depends(require_permission(PermCode.MODEL_MANAGE))) -> R:
    return R.ok(await model_service.test_connection(body))


# ------------------------------------------------------------------
# 模型用途（系统哪个位置用哪个模型）
# ------------------------------------------------------------------
@router.get("/model-roles")
async def list_model_roles(user: CurrentUser = Depends(require_permission(PermCode.MODEL_MANAGE))) -> R:
    return R.ok(await model_service.list_roles())


@router.put("/model-roles/{role}")
async def bind_model_role(role: str, body: RoleBindRequest,
                          user: CurrentUser = Depends(require_permission(PermCode.MODEL_MANAGE))) -> R:
    await model_service.bind_role(role, body.model_id)
    return R.ok(await model_service.list_roles(), "模型用途已更新")
