"""后台管理 REST 接口：普通用户账号 + 对话模型配置（全部要求管理员身份）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.deps import CurrentUser, require_admin
from app.core.response import R
from app.modules.admin import service as admin_service
from app.modules.admin.schemas import UserCreateRequest, UserUpdateRequest
from app.modules.model import service as model_service
from app.modules.model.schemas import ModelSaveRequest, ModelTestRequest, RoleBindRequest

router = APIRouter(prefix="/api/admin", tags=["后台管理"])


# ------------------------------------------------------------------
# 普通用户账号
# ------------------------------------------------------------------
@router.get("/users")
async def list_users(page: int = Query(1, ge=1), size: int = Query(10, ge=1, le=100),
                     keyword: str | None = None,
                     user: CurrentUser = Depends(require_admin)) -> R:
    return R.ok(await admin_service.list_users(page, size, keyword))


@router.post("/users")
async def create_user(body: UserCreateRequest,
                      user: CurrentUser = Depends(require_admin)) -> R:
    return R.ok(await admin_service.create_user(body), "用户已创建")


@router.put("/users/{user_id}")
async def update_user(user_id: int, body: UserUpdateRequest,
                      lock_version: int = Query(0, ge=0),
                      user: CurrentUser = Depends(require_admin)) -> R:
    return R.ok(await admin_service.update_user(user_id, body, lock_version), "用户已更新")


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, lock_version: int = Query(0, ge=0),
                      user: CurrentUser = Depends(require_admin)) -> R:
    await admin_service.delete_user(user_id, lock_version)
    return R.ok(None, "用户已删除")


# ------------------------------------------------------------------
# 对话模型配置
# ------------------------------------------------------------------
@router.get("/models")
async def list_models(user: CurrentUser = Depends(require_admin)) -> R:
    return R.ok(await model_service.list_models())


@router.post("/models")
async def create_model(body: ModelSaveRequest,
                       user: CurrentUser = Depends(require_admin)) -> R:
    return R.ok(await model_service.create_model(body), "模型已新增")


@router.put("/models/{model_id}")
async def update_model(model_id: int, body: ModelSaveRequest,
                       lock_version: int = Query(0, ge=0),
                       user: CurrentUser = Depends(require_admin)) -> R:
    return R.ok(await model_service.update_model(model_id, body, lock_version), "模型已更新")


@router.delete("/models/{model_id}")
async def delete_model(model_id: int, lock_version: int = Query(0, ge=0),
                       user: CurrentUser = Depends(require_admin)) -> R:
    await model_service.delete_model(model_id, lock_version)
    return R.ok(None, "模型已删除")


@router.post("/models/test")
async def test_model(body: ModelTestRequest,
                     user: CurrentUser = Depends(require_admin)) -> R:
    return R.ok(await model_service.test_connection(body))


# ------------------------------------------------------------------
# 模型用途（系统哪个位置用哪个模型）
# ------------------------------------------------------------------
@router.get("/model-roles")
async def list_model_roles(user: CurrentUser = Depends(require_admin)) -> R:
    return R.ok(await model_service.list_roles())


@router.put("/model-roles/{role}")
async def bind_model_role(role: str, body: RoleBindRequest,
                          user: CurrentUser = Depends(require_admin)) -> R:
    await model_service.bind_role(role, body.model_id)
    return R.ok(await model_service.list_roles(), "模型用途已更新")
