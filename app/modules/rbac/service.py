"""RBAC 与租户管理 Service。

职责：
- 启动时幂等初始化权限字典与内置角色；
- 解析账号的角色与权限（登录签发令牌 / 接口鉴权使用）；
- 租户、角色、成员角色的管理操作。

租户隔离说明：Account 标记了 __tenant_scoped__，成员查询会自动限制在
当前租户；Role/Permission 是平台字典（tenant_id=0 为内置），查询时显式
按「内置 + 本租户自建」过滤。
"""

from __future__ import annotations

from sqlalchemy import delete, select

from app.core.database import session_scope
from app.core.exceptions import NotFoundError, ParamError
from app.core.logging import get_logger
from app.models.entities import Account, AccountRole, Permission, Role, RolePermission, Tenant
from app.models.queries import base_count, base_filter
from app.modules.auth.password import hash_password
from app.modules.rbac.constants import (
    ALL, BUILTIN_ROLES, PERMISSION_CATALOG, RoleCode,
)
from app.modules.rbac.schemas import (
    RoleSaveRequest, RoleVO, TenantCreateRequest, TenantUpdateRequest, TenantVO,
)

logger = get_logger("app.rbac")


def _to_tenant_vo(t: Tenant) -> TenantVO:
    return TenantVO(id=t.id, code=t.code, name=t.name, status=t.status, plan=t.plan,
                    remark=t.remark, created_at=t.created_at)


def _role_vo(role: Role, permission_codes: list[str]) -> RoleVO:
    return RoleVO(id=role.id, tenant_id=role.tenant_id, code=role.code, name=role.name,
                  description=role.description, is_builtin=bool(role.is_builtin),
                  permission_codes=permission_codes)


# ------------------------------------------------------------------
# 初始化
# ------------------------------------------------------------------
async def ensure_rbac_seed() -> None:
    """幂等写入权限字典与内置角色（tenant_id=0，所有租户可复用）。"""
    async with session_scope() as session:
        existing_perms = {
            p.code: p for p in (await session.execute(select(Permission))).scalars().all()
        }
        for code, name, module in PERMISSION_CATALOG:
            if code not in existing_perms:
                perm = Permission(code=code, name=name, module=module)
                session.add(perm)
                existing_perms[code] = perm
        await session.flush()

        roles = (await session.execute(
            select(Role).where(Role.tenant_id == 0, Role.deleted == 0))).scalars().all()
        role_by_code = {r.code: r for r in roles}
        bindings = (await session.execute(
            select(RolePermission.role_id, Permission.code)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(RolePermission.deleted == 0)
        )).all()
        bound = {code: set() for code in role_by_code}
        for role_id, perm_code in bindings:
            for rc, r in role_by_code.items():
                if r.id == role_id:
                    bound[rc].add(perm_code)
                    break

        for code, name, description, perms in BUILTIN_ROLES:
            role = role_by_code.get(code)
            if role is None:
                role = Role(tenant_id=0, code=code, name=name, description=description,
                            is_builtin=1)
                session.add(role)
                await session.flush()
                role_by_code[code] = role
                bound[code] = set()
            if code == RoleCode.PLATFORM_ADMIN:
                continue   # 平台超管使用通配权限，不落绑定
            target = set(perms)
            current = bound.get(code, set())
            missing = target - current
            if missing:
                perm_ids = [existing_perms[c].id for c in missing if c in existing_perms]
                for pid in perm_ids:
                    session.add(RolePermission(role_id=role.id, permission_id=pid))
    logger.info("rbac_seed_ready")


# ------------------------------------------------------------------
# 账号鉴权信息
# ------------------------------------------------------------------
async def load_identity(account_id: int) -> tuple[list[str], list[str]]:
    """返回 (角色码列表, 权限码列表)；平台超管返回通配权限 ['*']。"""
    async with session_scope() as session:
        roles = (await session.execute(
            select(Role)
            .join(AccountRole, AccountRole.role_id == Role.id)
            .where(AccountRole.account_id == account_id,
                   AccountRole.deleted == 0, Role.deleted == 0))).scalars().all()
        role_codes = [r.code for r in roles]
        if RoleCode.PLATFORM_ADMIN in role_codes:
            return role_codes, [ALL]
        role_ids = [r.id for r in roles]
        if not role_ids:
            return role_codes, []
        perms = (await session.execute(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id.in_(role_ids), RolePermission.deleted == 0)
        )).scalars().all()
    return role_codes, sorted(set(perms))


# ------------------------------------------------------------------
# 角色
# ------------------------------------------------------------------
async def list_roles(tenant_id: int) -> list[RoleVO]:
    async with session_scope() as session:
        roles = (await session.execute(
            base_filter(Role).where(Role.tenant_id.in_([0, tenant_id])))).scalars().all()
        perms = (await session.execute(
            select(RolePermission.role_id, Permission.code)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(RolePermission.deleted == 0))).all()
        by_role: dict[int, list[str]] = {}
        for role_id, code in perms:
            by_role.setdefault(role_id, []).append(code)
        result = []
        for role in roles:
            codes = [ALL] if role.code == RoleCode.PLATFORM_ADMIN else sorted(by_role.get(role.id, []))
            result.append(_role_vo(role, codes))
    return result


async def create_role(tenant_id: int, req: RoleSaveRequest) -> RoleVO:
    if req.code in {r[0] for r in BUILTIN_ROLES}:
        raise ParamError("角色编码与内置角色冲突，请更换")
    async with session_scope() as session:
        dup = (await session.execute(
            base_filter(Role).where(Role.tenant_id == tenant_id, Role.code == req.code)
        )).scalar_one_or_none()
        if dup is not None:
            raise ParamError("该角色编码已存在")
        perm_ids = await _resolve_permission_ids(session, req.permission_codes)
        role = Role(tenant_id=tenant_id, code=req.code, name=req.name,
                    description=req.description, is_builtin=0)
        session.add(role)
        await session.flush()
        for pid in perm_ids:
            session.add(RolePermission(role_id=role.id, permission_id=pid))
        await session.flush()
        codes = sorted(req.permission_codes)
        return _role_vo(role, codes)


async def update_role(tenant_id: int, role_id: int, req: RoleSaveRequest) -> RoleVO:
    async with session_scope() as session:
        role = (await session.execute(
            base_filter(Role).where(Role.id == role_id, Role.tenant_id == tenant_id)
        )).scalar_one_or_none()
        if role is None:
            raise NotFoundError("角色不存在或不可修改（内置角色由平台维护）")
        perm_ids = await _resolve_permission_ids(session, req.permission_codes)
        role.name = req.name
        role.description = req.description
        await session.execute(
            delete(RolePermission).where(RolePermission.role_id == role.id))
        for pid in perm_ids:
            session.add(RolePermission(role_id=role.id, permission_id=pid))
        await session.flush()
        return _role_vo(role, sorted(req.permission_codes))


async def _resolve_permission_ids(session, codes: list[str]) -> list[int]:
    if not codes:
        return []
    rows = (await session.execute(
        base_filter(Permission).where(Permission.code.in_(codes)))).scalars().all()
    found = {r.code for r in rows}
    missing = set(codes) - found
    if missing:
        raise ParamError(f"未知权限点: {', '.join(sorted(missing))}")
    return [r.id for r in rows]


async def list_permissions() -> list[dict]:
    async with session_scope() as session:
        rows = (await session.execute(
            base_filter(Permission).order_by(Permission.module, Permission.code))).scalars().all()
    return [{"code": r.code, "name": r.name, "module": r.module} for r in rows]


async def role_ids_for_codes(session, role_codes: list[str], tenant_id: int) -> list[int]:
    """按角色码取 ID，限定为内置角色或本租户自建角色。"""
    if not role_codes:
        return []
    rows = (await session.execute(
        base_filter(Role).where(Role.code.in_(role_codes),
                                Role.tenant_id.in_([0, tenant_id])))).scalars().all()
    found = {r.code for r in rows}
    missing = set(role_codes) - found
    if missing:
        raise ParamError(f"未知角色: {', '.join(sorted(missing))}")
    return [r.id for r in rows]


async def assign_roles(session, account_id: int, role_ids: list[int],
                       tenant_id: int) -> None:
    """覆盖式设置成员角色；只允许绑定内置角色或本租户自建角色。调用方负责事务。"""
    account = (await session.execute(
        base_filter(Account).where(Account.id == account_id))).scalar_one_or_none()
    if account is None:
        raise NotFoundError("成员不存在")
    if role_ids:
        roles = (await session.execute(
            base_filter(Role).where(Role.id.in_(role_ids),
                                    Role.tenant_id.in_([0, tenant_id])))).scalars().all()
        if len(roles) != len(set(role_ids)):
            raise ParamError("包含不可分配的角色")
        if RoleCode.PLATFORM_ADMIN in {r.code for r in roles} and tenant_id != 0:
            raise ParamError("租户内不可分配平台超管角色")
    await session.execute(delete(AccountRole).where(AccountRole.account_id == account_id))
    for rid in role_ids:
        session.add(AccountRole(account_id=account_id, role_id=rid))
    await session.flush()


async def set_member_roles(account_id: int, role_ids: list[int], tenant_id: int) -> list[str]:
    """接口入口：独立事务内覆盖角色并返回最新角色码。"""
    async with session_scope() as session:
        await assign_roles(session, account_id, role_ids, tenant_id)
        roles = await load_roles(session, account_id)
    from app.modules.audit import service as audit
    await audit.record(module="rbac", action="role_assign", target_type="account",
                       target_id=account_id, tenant_id=tenant_id,
                       detail={"roles": roles})
    return roles


async def load_roles(session, account_id: int) -> list[str]:
    codes = (await session.execute(
        select(Role.code)
        .join(AccountRole, AccountRole.role_id == Role.id)
        .where(AccountRole.account_id == account_id,
               AccountRole.deleted == 0, Role.deleted == 0))).scalars().all()
    return sorted(set(codes))


# ------------------------------------------------------------------
# 租户
# ------------------------------------------------------------------
async def list_tenants(page: int, size: int, keyword: str | None) -> dict:
    async with session_scope() as session:
        stmt = base_filter(Tenant).where(Tenant.id != 0)
        count_stmt = base_count(Tenant).where(Tenant.id != 0)
        if keyword:
            stmt = stmt.where(Tenant.code.like(f"%{keyword}%") | Tenant.name.like(f"%{keyword}%"))
            count_stmt = count_stmt.where(Tenant.code.like(f"%{keyword}%") | Tenant.name.like(f"%{keyword}%"))
        total = (await session.execute(count_stmt)).scalar_one()
        rows = (await session.execute(
            stmt.order_by(Tenant.id.desc()).offset((page - 1) * size).limit(size))).scalars().all()
    return {"total": total, "page_num": page, "page_size": size,
            "records": [_to_tenant_vo(t) for t in rows]}


async def create_tenant(req: TenantCreateRequest) -> TenantVO:
    async with session_scope() as session:
        dup = (await session.execute(
            base_filter(Tenant).where(Tenant.code == req.code))).scalar_one_or_none()
        if dup is not None:
            raise ParamError("租户编码已存在")
        tenant = Tenant(code=req.code, name=req.name, status="ACTIVE", plan=req.plan,
                        remark=req.remark)
        session.add(tenant)
        await session.flush()

        # 初始化租户管理员（工号登录），并绑定租户管理员角色
        role_id = (await session.execute(
            base_filter(Role).where(Role.tenant_id == 0, Role.code == RoleCode.TENANT_ADMIN)
        )).scalar_one().id
        account = Account(tenant_id=tenant.id, login_id=req.admin_login_id, login_type="EMP",
                          password=hash_password(req.admin_password),
                          name=req.admin_name or req.admin_login_id, status="ACTIVE")
        session.add(account)
        await session.flush()
        session.add(AccountRole(account_id=account.id, role_id=role_id))
        await session.flush()
        vo = _to_tenant_vo(tenant)
    from app.modules.audit import service as audit
    await audit.record(module="tenant", action="tenant_create", target_type="tenant",
                       target_id=tenant.id, tenant_id=tenant.id,
                       detail={"code": tenant.code, "name": tenant.name,
                               "admin_login_id": req.admin_login_id})
    return vo


async def update_tenant(tenant_id: int, req: TenantUpdateRequest) -> TenantVO:
    async with session_scope() as session:
        tenant = (await session.execute(
            base_filter(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
        if tenant is None or tenant.id == 0:
            raise NotFoundError("租户不存在")
        values = req.model_dump(exclude_unset=True)
        for key, value in values.items():
            if value is not None:
                setattr(tenant, key, value)
        await session.flush()
        vo = _to_tenant_vo(tenant)
    from app.modules.audit import service as audit
    await audit.record(module="tenant", action="tenant_update", target_type="tenant",
                       target_id=tenant_id, tenant_id=tenant_id, detail=values)
    return vo


async def get_tenant_by_code(code: str) -> Tenant | None:
    async with session_scope() as session:
        return (await session.execute(
            base_filter(Tenant).where(Tenant.code == code))).scalar_one_or_none()


async def load_tenant_of_account(account_id: int) -> int:
    """平台超管操作成员角色时，按账号回查其所属租户。"""
    async with session_scope() as session:
        account = (await session.execute(
            base_filter(Account).where(Account.id == account_id))).scalar_one_or_none()
    if account is None:
        raise NotFoundError("成员不存在")
    return account.tenant_id


async def tenant_stats(tenant_id: int) -> dict:
    """租户概览：成员数（供平台超管列表展示）。"""
    async with session_scope() as session:
        total = (await session.execute(
            base_count(Account).where(Account.tenant_id == tenant_id))).scalar_one()
    return {"member_count": total}
