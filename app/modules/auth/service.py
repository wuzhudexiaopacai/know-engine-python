"""认证 Service：口令校验 + 签名令牌。

令牌选型：itsdangerous URLSafeTimedSerializer（HMAC-SHA256 签名 + 时间戳），
无状态、可校验过期，避免引入 JWT 库；载荷仅含身份标识，敏感信息不入令牌。
"""

from __future__ import annotations

import base64
import io
import random
import secrets
from typing import Literal

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.config.settings import get_settings
from app.core.database import session_scope
from app.core.exceptions import AuthError, LoginError, NotFoundError, ParamError
from app.core.logging import get_logger
from app.models.constants import AdminStatus, UserStatus
from app.models.entities import AdminInfo, UserInfo
from app.models.queries import base_filter, update_with_lock
from app.infra.cache import get_cache
from app.infra.file_storage import get_file_store
from app.modules.auth.password import verify_password
from app.modules.auth.schemas import ProfileUpdateRequest, ProfileVO

logger = get_logger("app.auth")

Identity = Literal["user", "admin"]


# ------------------------------------------------------------------
# 图形验证码
# ------------------------------------------------------------------
_CAPTCHA_TTL = 300.0
_CAPTCHA_LENGTH = 4
_CAPTCHA_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 去掉易混淆的 I/O/0/1
_CAPTCHA_SIZE = (132, 44)


def _captcha_font(size: int):
    """优先 TrueType 字体保证可读性；精简容器缺字体时退回 Pillow 内置字体。"""
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except TypeError:                             # pragma: no cover - 旧版 Pillow
        return ImageFont.load_default()


def _render_captcha(code: str) -> bytes:
    """内存绘制验证码 PNG（干扰线 + 噪点），不落盘、不依赖外部服务。"""
    width, height = _CAPTCHA_SIZE
    image = Image.new("RGB", (width, height), (244, 247, 252))
    draw = ImageDraw.Draw(image)
    for _ in range(3):
        draw.line(
            [(random.randint(0, width), random.randint(0, height)),
             (random.randint(0, width), random.randint(0, height))],
            fill=(random.randint(150, 200), random.randint(160, 205), random.randint(180, 220)),
        )
    for _ in range(80):
        draw.point((random.randint(0, width), random.randint(0, height)),
                   fill=(random.randint(160, 205), random.randint(160, 205), random.randint(180, 220)))
    font = _captcha_font(26)
    step = width // (_CAPTCHA_LENGTH + 1)
    for index, char in enumerate(code):
        draw.text((step * (index + 1) - 11, random.randint(4, 10)), char, font=font,
                  fill=(random.randint(20, 70), random.randint(45, 95), random.randint(120, 185)))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def create_captcha() -> dict:
    """签发一次性验证码：答案存缓存（TTL 300 秒），接口只回图片。"""
    captcha_id = secrets.token_urlsafe(24)
    code = "".join(secrets.choice(_CAPTCHA_CHARS) for _ in range(_CAPTCHA_LENGTH))
    png = await run_in_threadpool(_render_captcha, code)
    await get_cache().set_raw(f"captcha:{captcha_id}", code, _CAPTCHA_TTL)
    logger.info("captcha_issued", captcha_id=captcha_id[:8])
    return {
        "captcha_id": captcha_id,
        "image": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        "expires_in": int(_CAPTCHA_TTL),
    }


async def verify_captcha(captcha_id: str, code: str) -> None:
    """校验并立即作废验证码：无论成败都删除，避免重放。"""
    cache = get_cache()
    key = f"captcha:{captcha_id}"
    expected = await cache.get_raw(key)
    await cache.delete(key)
    if not expected or (code or "").strip().upper() != str(expected).strip().upper():
        raise LoginError("验证码错误或已过期，请刷新后重试")


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().app.secret_key, salt="know-engine-auth")


# ------------------------------------------------------------------
# 令牌
# ------------------------------------------------------------------
def issue_token(subject_id: str, identity: Identity, name: str) -> str:
    """签发令牌：载荷 = {sub, identity, name}。"""
    return _serializer().dumps({"sub": subject_id, "identity": identity, "name": name})


def verify_token(token: str) -> dict:
    """校验并解析令牌；非法/过期一律 AuthError（401）。"""
    max_age = get_settings().app.token_ttl_seconds
    try:
        data = _serializer().loads(token, max_age=max_age)
    except SignatureExpired:
        raise AuthError("登录已过期，请重新登录") from None
    except BadSignature:
        raise AuthError("登录状态无效，请重新登录") from None
    if not isinstance(data, dict) or "sub" not in data:
        raise AuthError("登录状态无效")
    return data


# ------------------------------------------------------------------
# 登录
# ------------------------------------------------------------------
_LOGIN_FAIL_MAX = 5          # 窗口内允许的最大失败次数
_LOGIN_FAIL_WINDOW = 600.0   # 失败计数窗口（秒）


def _fail_key(identity: str, account: str) -> str:
    return f"auth:fail:{identity}:{account}"


async def _check_login_throttle(identity: str, account: str) -> None:
    """失败次数超限直接拒绝：防口令爆破（验证码之上再加一道）。"""
    count = await get_cache().get_raw(_fail_key(identity, account))
    if int(count or 0) >= _LOGIN_FAIL_MAX:
        raise LoginError("登录失败次数过多，请 10 分钟后再试")


async def _record_login_failure(identity: str, account: str) -> None:
    count = await get_cache().incr_raw(_fail_key(identity, account), _LOGIN_FAIL_WINDOW)
    logger.warning("login_failed", identity=identity, account=account, count=count)
    if count >= _LOGIN_FAIL_MAX:
        raise LoginError("登录失败次数过多，请 10 分钟后再试")


async def login_user(phone: str, password: str, *, captcha_id: str, captcha_code: str) -> dict:
    await verify_captcha(captcha_id, captcha_code)
    await _check_login_throttle("user", phone)
    async with session_scope() as session:
        user = (await session.execute(
            base_filter(UserInfo).where(UserInfo.phone == phone))).scalar_one_or_none()
    if user is None or not verify_password(password, user.password):
        await _record_login_failure("user", phone)
        # 统一话术，避免账号枚举
        raise LoginError("手机号或密码错误")
    if user.status != UserStatus.ACTIVE:
        raise LoginError("账号已停用，请联系管理员")
    await get_cache().delete(_fail_key("user", phone))
    token = issue_token(str(user.id), "user", user.nickname or user.name or phone)
    logger.info("user_login", user_id=user.id)
    return {"token": token, "identity": "user",
            "user": {"id": str(user.id), "name": user.name, "nickname": user.nickname,
                     "avatar": user.avatar, "motto": user.motto}}


async def login_admin(emp_id: str, password: str, *, captcha_id: str, captcha_code: str) -> dict:
    await verify_captcha(captcha_id, captcha_code)
    await _check_login_throttle("admin", emp_id)
    async with session_scope() as session:
        admin = (await session.execute(
            base_filter(AdminInfo).where(AdminInfo.emp_id == emp_id))).scalar_one_or_none()
    if admin is None or not admin.password or not verify_password(password, admin.password):
        await _record_login_failure("admin", emp_id)
        raise LoginError("工号或密码错误")
    if admin.status != AdminStatus.ON_JOB:
        raise LoginError("账号已停用，请联系管理员")
    await get_cache().delete(_fail_key("admin", emp_id))
    token = issue_token(str(admin.id), "admin", admin.name or emp_id)
    logger.info("admin_login", emp_id=emp_id)
    return {"token": token, "identity": "admin",
            "user": {"id": str(admin.id), "emp_id": admin.emp_id,
                     "name": admin.name, "job": admin.job,
                     "avatar": admin.avatar, "motto": admin.motto}}


# ------------------------------------------------------------------
# 个人资料（用户/管理员均可设置头像与座右铭）
# ------------------------------------------------------------------
_MAX_AVATAR_BYTES = 2 * 1024 * 1024


def _entity(identity: str):
    if identity == "admin":
        return AdminInfo
    if identity == "user":
        return UserInfo
    raise AuthError("请先登录")


def _int_sub(sub: str) -> int:
    try:
        return int(sub)
    except (TypeError, ValueError):
        raise NotFoundError("账号不存在") from None


def _to_profile(row, identity: str) -> ProfileVO:
    if identity == "admin":
        return ProfileVO(id=row.id, identity="admin", name=row.name,
                         avatar=row.avatar, motto=row.motto, lock_version=row.lock_version)
    return ProfileVO(id=row.id, identity="user", name=row.name, nickname=row.nickname,
                     avatar=row.avatar, motto=row.motto, lock_version=row.lock_version)


async def get_profile(identity: str, sub: str) -> ProfileVO:
    entity = _entity(identity)
    pk = _int_sub(sub)
    async with session_scope() as session:
        row = (await session.execute(
            base_filter(entity).where(entity.id == pk))).scalar_one_or_none()
    if row is None:
        raise NotFoundError("账号不存在或已被删除")
    return _to_profile(row, identity)


async def update_profile(identity: str, sub: str, req: ProfileUpdateRequest) -> ProfileVO:
    """个人资料更新：用户可改昵称，管理员可改姓名；头像/座右铭双方都可设置。"""
    entity = _entity(identity)
    pk = _int_sub(sub)
    values = req.model_dump(exclude_unset=True)
    expected_lock = values.pop("lock_version", None)
    values.pop("name" if identity != "admin" else "nickname", None)

    async with session_scope() as session:
        row = (await session.execute(
            base_filter(entity).where(entity.id == pk))).scalar_one_or_none()
        if row is None:
            raise NotFoundError("账号不存在或已被删除")
        if values:
            if expected_lock is not None:
                await update_with_lock(session, entity, "id", pk, values, expected_lock)
            else:
                for field, value in values.items():
                    setattr(row, field, value)
                await session.flush()
            await session.refresh(row)
        vo = _to_profile(row, identity)
    return vo


async def save_avatar(filename: str | None, content_type: str | None, data: bytes) -> str:
    """头像上传：校验图片类型与大小后入对象存储，返回可长期访问的稳定地址。"""
    if not content_type or not content_type.startswith("image/"):
        raise ParamError("仅支持上传图片作为头像（png/jpg/webp 等）")
    if not data:
        raise ParamError("头像文件为空")
    if len(data) > _MAX_AVATAR_BYTES:
        raise ParamError("头像图片不能超过 2MB")
    key = await get_file_store().put("avatars", filename or "avatar.png", data)
    logger.info("avatar_saved", key=key, size=len(data))
    return f"/api/files/{key}"
