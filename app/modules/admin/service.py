"""后台管理 Service：成员账号的增删改查与角色分配。

租户边界：Account 标记了 __tenant_scoped__，会话按当前登录租户自动过滤；
平台超管（tenant_id=None）跨租户操作时必须显式传入 tenant_id。
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.core.database import session_scope
from app.core.exceptions import NotFoundError, ParamError
from app.models.entities import Account, AccountRole, Role
from app.models.queries import base_count, base_filter, soft_delete, update_with_lock
from app.modules.admin.schemas import UserCreateRequest, UserListVO, UserUpdateRequest, UserVO
from app.modules.auth.password import hash_password
from app.modules.rbac import service as rbac


def _to_vo(account: Account, roles: list[str]) -> UserVO:
    return UserVO(id=account.id, tenant_id=account.tenant_id, login_id=account.login_id,
                  login_type=account.login_type, name=account.name, nickname=account.nickname,
                  avatar=account.avatar, job=account.job, status=account.status,
                  roles=roles, lock_version=account.lock_version, created_at=account.created_at)


async def _role_map(session, account_ids: list[int]) -> dict[int, list[str]]:
    """批量取账号角色，避免列表接口 N+1 查询。"""
    if not account_ids:
        return {}
    rows = (await session.execute(
        select(AccountRole.account_id, Role.code)
        .join(Role, Role.id == AccountRole.role_id)
        .where(AccountRole.account_id.in_(account_ids),
               AccountRole.deleted == 0, Role.deleted == 0))).all()
    result: dict[int, list[str]] = {}
    for account_id, code in rows:
        result.setdefault(account_id, []).append(code)
    return result


async def create_user(req: UserCreateRequest, tenant_id: int | None) -> UserVO:
    if tenant_id is None:
        raise ParamError("请先切换到目标租户或指定 tenant_id")
    async with session_scope() as session:
        dup = (await session.execute(
            base_filter(Account).where(Account.tenant_id == tenant_id,
                                       Account.login_type == req.login_type,
                                       Account.login_id == req.login_id))).scalar_one_or_none()
        if dup is not None:
            raise ParamError("该登录标识在本租户已存在")
        account = Account(tenant_id=tenant_id, login_id=req.login_id,
                          login_type=req.login_type, password=hash_password(req.password),
                          name=req.name or req.login_id, nickname=req.nickname,
                          job=req.job, status="ACTIVE")
        session.add(account)
        await session.flush()
        await rbac.assign_roles(session, account.id,
                                await rbac.role_ids_for_codes(session, req.role_codes, tenant_id),
                                tenant_id)
        await session.refresh(account)
        return _to_vo(account, await rbac.load_roles(session, account.id))


async def update_user(user_id: int, req: UserUpdateRequest, lock_version: int,
                      tenant_id: int | None) -> UserVO:
    """编辑成员：登录标识/资料/密码/状态/角色按需更新。"""
    async with session_scope() as session:
        row = (await session.execute(
            base_filter(Account).where(Account.id == user_id))).scalar_one_or_none()
        if row is None:
            raise NotFoundError("成员不存在或已被删除")
        values = req.model_dump(exclude_unset=True, exclude={"role_codes"})
        values = {k: v for k, v in values.items() if v is not None}
        if values.get("login_id") and values["login_id"] != row.login_id:
            dup = (await session.execute(
                base_filter(Account).where(Account.tenant_id == row.tenant_id,
                                           Account.login_type == row.login_type,
                                           Account.login_id == values["login_id"])
            )).scalar_one_or_none()
            if dup is not None:
                raise ParamError("该登录标识已被其他成员占用")
        if values.get("password"):
            values["password"] = hash_password(values["password"])
        if values:
            await update_with_lock(session, Account, "id", user_id, values, lock_version)
        if req.role_codes is not None:
            await rbac.assign_roles(
                session, user_id,
                await rbac.role_ids_for_codes(session, req.role_codes, row.tenant_id),
                row.tenant_id)
        row = (await session.execute(
            base_filter(Account).where(Account.id == user_id))).scalar_one()
        role_map = await _role_map(session, [row.id])
        return _to_vo(row, sorted(role_map.get(row.id, [])))


async def delete_user(user_id: int, lock_version: int) -> None:
    """逻辑删除：保留历史会话可审计，登录与列表立即不可见。"""
    async with session_scope() as session:
        row = (await session.execute(
            base_filter(Account).where(Account.id == user_id))).scalar_one_or_none()
        if row is None:
            raise NotFoundError("成员不存在或已被删除")
        await soft_delete(session, Account, "id", user_id, lock_version)


async def list_users(page: int, size: int, keyword: str | None, tenant_id: int | None) -> UserListVO:
    async with session_scope() as session:
        stmt = base_filter(Account)
        count_stmt = base_count(Account)
        if tenant_id is not None:
            stmt = stmt.where(Account.tenant_id == tenant_id)
            count_stmt = count_stmt.where(Account.tenant_id == tenant_id)
        if keyword:
            cond = (Account.login_id.like(f"%{keyword}%")
                    | Account.name.like(f"%{keyword}%")
                    | Account.nickname.like(f"%{keyword}%"))
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)
        total = (await session.execute(count_stmt)).scalar_one()
        rows = (await session.execute(
            stmt.order_by(Account.id.desc())
            .offset((page - 1) * size).limit(size))).scalars().all()
        role_map = await _role_map(session, [r.id for r in rows])
    return UserListVO(total=total, page=page, page_size=size,
                      records=[_to_vo(r, sorted(role_map.get(r.id, []))) for r in rows])
