"""认证 REST 接口：用户手机号登录 / 管理员登录（工号） / 当前身份与个人资料。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, get_current_user
from app.core.exceptions import AuthError
from app.core.response import R
from app.modules.auth import service as auth
from app.modules.auth.schemas import ProfileUpdateRequest

router = APIRouter(prefix="/api/auth", tags=["认证"])


def _require_login(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """个人资料类接口：游客不可访问。"""
    if user.is_guest:
        raise AuthError("请先登录")
    return user


class UserLogin(BaseModel):
    tenant_code: str = Field(min_length=1, max_length=32)
    phone: str = Field(pattern=r"^1\d{10}$")
    password: str = Field(min_length=1, max_length=64)
    captcha_id: str = Field(min_length=1, max_length=64)
    captcha_code: str = Field(min_length=1, max_length=8)


class AdminLogin(BaseModel):
    tenant_code: str = Field(min_length=1, max_length=32)
    emp_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=64)
    captcha_id: str = Field(min_length=1, max_length=64)
    captcha_code: str = Field(min_length=1, max_length=8)


@router.get("/captcha")
async def captcha() -> R:
    """签发图形验证码：图片以 base64 data URL 返回，答案服务端缓存、一次性有效。"""
    return R.ok(await auth.create_captcha())


@router.post("/login")
async def login(body: UserLogin) -> R:
    return R.ok(await auth.login_user(body.phone, body.password, tenant_code=body.tenant_code,
                                      captcha_id=body.captcha_id,
                                      captcha_code=body.captcha_code), "登录成功")


@router.post("/admin-login")
async def admin_login(body: AdminLogin) -> R:
    return R.ok(await auth.login_admin(body.emp_id, body.password, tenant_code=body.tenant_code,
                                       captcha_id=body.captcha_id,
                                       captcha_code=body.captcha_code), "登录成功")


@router.get("/me")
async def me(user: CurrentUser = Depends(get_current_user)) -> R:
    if user.is_guest:
        return R.ok({"sub": user.sub, "identity": "guest", "name": user.name,
                     "avatar": None, "motto": None, "tenant": None,
                     "roles": [], "permissions": []})
    profile = await auth.get_profile(user.identity, user.sub)
    return R.ok({"sub": user.sub, "identity": profile.identity,
                 "name": profile.name, "nickname": profile.nickname,
                 "avatar": profile.avatar, "motto": profile.motto,
                 "lock_version": profile.lock_version,
                 "tenant": {"id": user.tenant_id, "code": user.tenant_code},
                 "roles": list(user.roles), "permissions": sorted(user.permissions)})


@router.get("/profile")
async def get_profile(user: CurrentUser = Depends(_require_login)) -> R:
    return R.ok(await auth.get_profile(user.identity, user.sub))


@router.put("/profile")
async def update_profile(body: ProfileUpdateRequest,
                         user: CurrentUser = Depends(_require_login)) -> R:
    return R.ok(await auth.update_profile(user.identity, user.sub, body), "资料已保存")


@router.post("/avatar")
async def upload_avatar(file: UploadFile = File(...),
                        user: CurrentUser = Depends(_require_login)) -> R:
    data = await file.read()
    return R.ok({"url": await auth.save_avatar(file.filename, file.content_type, data)},
                "头像已上传")
