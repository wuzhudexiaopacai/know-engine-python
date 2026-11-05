"""后台管理入参/出参契约（成员账号 + 模型配置）。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class UserCreateRequest(BaseModel):
    """新增成员账号：手机号（PHONE）或工号（EMP），角色按 RBAC 分配。"""

    login_id: str = Field(min_length=3, max_length=64)
    login_type: Literal["PHONE", "EMP"] = "PHONE"
    password: str = Field(min_length=6, max_length=64)
    name: str | None = Field(default=None, max_length=64)
    nickname: str | None = Field(default=None, max_length=128)
    job: str | None = Field(default=None, max_length=128)
    role_codes: list[str] = Field(default_factory=lambda: ["member"])
    tenant_id: int | None = Field(default=None, description="平台超管跨租户创建时指定")


class UserUpdateRequest(BaseModel):
    """编辑成员：字段按需传，password 为空表示不改密码。"""

    login_id: str | None = Field(default=None, min_length=3, max_length=64)
    name: str | None = Field(default=None, max_length=64)
    nickname: str | None = Field(default=None, max_length=128)
    job: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, min_length=6, max_length=64)
    status: Literal["ACTIVE", "DISABLED"] | None = None
    role_codes: list[str] | None = None


class UserVO(BaseModel):
    id: int
    tenant_id: int
    login_id: str
    login_type: str
    name: str | None = None
    nickname: str | None = None
    avatar: str | None = None
    job: str | None = None
    status: str
    roles: list[str] = Field(default_factory=list)
    lock_version: int = 0
    created_at: datetime


class UserListVO(BaseModel):
    total: int
    page: int
    page_size: int
    records: list[UserVO]
