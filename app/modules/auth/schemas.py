"""认证与个人资料契约。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProfileVO(BaseModel):
    """当前登录者的个人资料（用户与管理员共用视图）。"""

    id: int
    identity: str
    name: str | None = None
    nickname: str | None = None
    avatar: str | None = None
    motto: str | None = None
    lock_version: int = 0


class ProfileUpdateRequest(BaseModel):
    """个人资料更新（仅传需要修改的字段）。"""

    name: str | None = Field(default=None, max_length=64)
    nickname: str | None = Field(default=None, max_length=128)
    motto: str | None = Field(default=None, max_length=255)
    avatar: str | None = Field(default=None, max_length=512)
    lock_version: int | None = Field(default=None, ge=0)


class AvatarUploadResult(BaseModel):
    url: str
