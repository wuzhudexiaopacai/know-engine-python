"""后台管理 Service：普通用户账号的增删改查（禁用/启用/编辑/删除）。"""

from __future__ import annotations

from sqlalchemy import func, select

from app.core.database import session_scope
from app.core.exceptions import NotFoundError, ParamError
from app.models.entities import UserInfo
from app.models.queries import base_filter, soft_delete, update_with_lock
from app.modules.admin.schemas import UserCreateRequest, UserListVO, UserUpdateRequest, UserVO
from app.modules.auth.password import hash_password


def _to_vo(u: UserInfo) -> UserVO:
    return UserVO(id=u.id, phone=u.phone, name=u.name, nickname=u.nickname,
                  avatar=u.avatar, status=u.status, lock_version=u.lock_version,
                  created_at=u.created_at)


async def create_user(req: UserCreateRequest) -> UserVO:
    async with session_scope() as session:
        exists = (await session.execute(
            base_filter(UserInfo).where(UserInfo.phone == req.phone))).scalar_one_or_none()
        if exists is not None:
            raise ParamError("该手机号已存在，请更换或直接使用原账号")
        user = UserInfo(phone=req.phone, password=hash_password(req.password),
                        name=req.name or req.phone, nickname=req.nickname,
                        status="ACTIVE")
        session.add(user)
        await session.flush()
        # created_at 为服务端默认值，flush 后需刷新才能读到
        await session.refresh(user)
        vo = _to_vo(user)
    return vo


async def update_user(user_id: int, req: UserUpdateRequest, lock_version: int) -> UserVO:
    """编辑用户：手机号/姓名/昵称/密码/状态按需更新，密码留空则不变。"""
    async with session_scope() as session:
        row = (await session.execute(
            base_filter(UserInfo).where(UserInfo.id == user_id))).scalar_one_or_none()
        if row is None:
            raise NotFoundError("用户不存在或已被删除")
        values = req.model_dump(exclude_unset=True)
        if not values.get("phone"):
            values.pop("phone", None)   # 手机号不可置空
        # 手机号变更需全局唯一校验
        if values.get("phone") and values["phone"] != row.phone:
            dup = (await session.execute(
                base_filter(UserInfo).where(UserInfo.phone == values["phone"])
            )).scalar_one_or_none()
            if dup is not None:
                raise ParamError("该手机号已被其他用户占用")
        if values.get("password"):
            values["password"] = hash_password(values["password"])
        else:
            values.pop("password", None)
        if values:
            await update_with_lock(session, UserInfo, "id", user_id, values, lock_version)
            row = (await session.execute(
                base_filter(UserInfo).where(UserInfo.id == user_id))).scalar_one()
        vo = _to_vo(row)
    return vo


async def delete_user(user_id: int, lock_version: int) -> None:
    """逻辑删除：保留历史数据可审计，登录与列表立即不可见。"""
    async with session_scope() as session:
        row = (await session.execute(
            base_filter(UserInfo).where(UserInfo.id == user_id))).scalar_one_or_none()
        if row is None:
            raise NotFoundError("用户不存在或已被删除")
        await soft_delete(session, UserInfo, "id", user_id, lock_version)


async def list_users(page: int, size: int, keyword: str | None) -> UserListVO:
    async with session_scope() as session:
        stmt = base_filter(UserInfo)
        count_stmt = select(func.count()).select_from(UserInfo).where(UserInfo.deleted == 0)
        if keyword:
            stmt = stmt.where(UserInfo.phone.like(f"%{keyword}%"))
            count_stmt = count_stmt.where(UserInfo.phone.like(f"%{keyword}%"))
        total = (await session.execute(count_stmt)).scalar_one()
        rows = (await session.execute(
            stmt.order_by(UserInfo.id.desc())
            .offset((page - 1) * size).limit(size))).scalars().all()
    return UserListVO(total=total, page=page, page_size=size,
                      records=[_to_vo(u) for u in rows])
