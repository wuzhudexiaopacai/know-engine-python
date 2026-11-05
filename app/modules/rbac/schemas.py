"""租户与 RBAC 的入参/出参契约。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

TENANT_CODE_PATTERN = r"^[a-z][a-z0-9_-]{2,31}$"


class TenantCreateRequest(BaseModel):
    code: str = Field(pattern=TENANT_CODE_PATTERN, description="租户编码（登录时输入）")
    name: str = Field(min_length=1, max_length=128)
    plan: str = Field(default="STANDARD", max_length=32)
    remark: str | None = Field(default=None, max_length=255)
    admin_login_id: str = Field(min_length=3, max_length=64, description="租户管理员工号")
    admin_name: str | None = Field(default=None, max_length=64)
    admin_password: str = Field(min_length=6, max_length=64)


class TenantUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    status: str | None = Field(default=None, pattern="^(ACTIVE|DISABLED)$")
    plan: str | None = Field(default=None, max_length=32)
    remark: str | None = Field(default=None, max_length=255)


class TenantVO(BaseModel):
    id: int
    code: str
    name: str
    status: str
    plan: str
    remark: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class RoleSaveRequest(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=255)
    permission_codes: list[str] = Field(default_factory=list)


class RoleVO(BaseModel):
    id: int
    tenant_id: int
    code: str
    name: str
    description: str | None = None
    is_builtin: bool
    permission_codes: list[str] = Field(default_factory=list)


class RoleAssignRequest(BaseModel):
    role_ids: list[int] = Field(default_factory=list)
