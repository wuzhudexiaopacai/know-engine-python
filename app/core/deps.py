"""FastAPI 依赖：当前登录身份解析与 RBAC 鉴权。

令牌来源二选一（兼容 SSE/EventSource 无法自定义头的场景）：
    1. Authorization: Bearer <token>   —— fetch 流式与常规 REST
    2. ?token=<token>                  —— 浏览器原生 EventSource 兜底

未携带令牌时按访客（VISITOR）放行；管理接口用 require_permission 强制校验。
解析身份时同步写入租户上下文，后续所有 ORM 查询自动按 tenant_id 隔离。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Depends, Header, HTTPException, Query

from app.core.tenant import DEFAULT_TENANT_ID, set_current_tenant
from app.modules.auth import service as auth
from app.modules.rbac.constants import ALL, RoleCode


@dataclass
class CurrentUser:
    sub: str
    identity: str      # user / admin / guest（展示用身份，权限由 roles/permissions 决定）
    name: str
    tenant_id: int | None
    tenant_code: str = "default"
    roles: tuple[str, ...] = ()
    permissions: frozenset[str] = field(default_factory=frozenset)
    # 检索权限身份：游客=VISITOR，其余透传工号/手机号，供 accessibleBy filter 使用
    identity_for_retrieval: str = "VISITOR"
    is_guest: bool = False

    @property
    def owner_key(self) -> str:
        """会话归属键：统一账号 id 全局唯一，加身份前缀保持存量会话可读。"""
        return f"{self.identity}:{self.sub}"

    @property
    def is_platform_admin(self) -> bool:
        return RoleCode.PLATFORM_ADMIN in self.roles

    def has(self, permission: str) -> bool:
        return ALL in self.permissions or permission in self.permissions


def _resolve(token: str | None) -> CurrentUser:
    if not token:
        set_current_tenant(DEFAULT_TENANT_ID)
        return CurrentUser(sub="anonymous", identity="guest", name="游客",
                           tenant_id=DEFAULT_TENANT_ID, permissions=frozenset(),
                           identity_for_retrieval="VISITOR", is_guest=True)
    data = auth.verify_token(token)
    roles = tuple(data.get("roles") or ())
    tid = int(data.get("tid", DEFAULT_TENANT_ID))
    # 平台超管需要跨租户视角，不做租户过滤；其余账号只看到本租户数据
    set_current_tenant(None if RoleCode.PLATFORM_ADMIN in roles else tid)
    return CurrentUser(
        sub=str(data["sub"]), identity=data.get("identity", "user"),
        name=data.get("name", ""), tenant_id=tid,
        tenant_code=data.get("tcode", "default"), roles=roles,
        permissions=frozenset(data.get("perms") or ()),
        identity_for_retrieval=str(data.get("rid") or "VISITOR"),
    )


async def get_current_user(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> CurrentUser:
    raw = token
    if not raw and authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    return _resolve(raw)


def require_permission(*permissions: str):
    """权限点守卫：满足任意一个权限即放行（平台超管的通配 `*` 视为全部满足）。"""

    async def _guard(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.is_guest:
            raise HTTPException(status_code=401, detail="需要登录")
        if not any(user.has(p) for p in permissions):
            raise HTTPException(status_code=403, detail="没有操作权限")
        return user

    return _guard


async def require_platform_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.is_guest:
        raise HTTPException(status_code=401, detail="需要登录")
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="需要平台超管权限")
    return user
