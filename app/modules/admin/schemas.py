"""后台管理入参/出参契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class UserCreateRequest(BaseModel):
    """管理员新增普通用户账号（手机号登录，仅可对话）。"""

    phone: str = Field(pattern=r"^1\d{10}$", description="11 位手机号，全局唯一")
    password: str = Field(min_length=6, max_length=64)
    name: str | None = Field(default=None, max_length=64)
    nickname: str | None = Field(default=None, max_length=128)


class UserUpdateRequest(BaseModel):
    """管理员编辑普通用户：字段按需传，password 为空表示不改密码。"""

    phone: str | None = Field(default=None, pattern=r"^1\d{10}$")
    name: str | None = Field(default=None, max_length=64)
    nickname: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, min_length=6, max_length=64)
    status: Literal["ACTIVE", "DISABLED"] | None = None


class UserVO(BaseModel):
    id: int
    phone: str
    name: str | None = None
    nickname: str | None = None
    avatar: str | None = None
    status: str
    lock_version: int = 0
    created_at: datetime


class UserListVO(BaseModel):
    total: int
    page: int
    page_size: int
    records: list[UserVO]
