"""FastAPI 依赖：当前登录身份解析。

令牌来源二选一（兼容 SSE/EventSource 无法自定义头的场景）：
    1. Authorization: Bearer <token>   —— fetch 流式与常规 REST
    2. ?token=<token>                  —— 浏览器原生 EventSource 兜底
未携带令牌时按访客（VISITOR）放行，仅管理接口用 require_admin 强制校验。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException, Query

from app.modules.auth import service as auth


@dataclass
class CurrentUser:
    sub: str
    identity: str      # user / admin / guest
    name: str
    # 检索权限身份：游客=VISITOR，其余透传工号/用户ID，供 accessibleBy filter 使用
    identity_for_retrieval: str

    @property
    def owner_key(self) -> str:
        """会话归属键：用户表与管理员表各自自增，裸 id 会撞车（1 号用户 == 1 号管理员），
        加身份前缀形成全局唯一命名空间，保证会话数据按人隔离。"""
        return f"{self.identity}:{self.sub}"


def _resolve(token: str | None) -> CurrentUser:
    if not token:
        return CurrentUser(sub="anonymous", identity="guest", name="游客",
                           identity_for_retrieval="VISITOR")
    data = auth.verify_token(token)
    identity = data.get("identity", "user")
    # 管理员透传工号、用户透传手机号，命中 accessibleBy 的专属文档才可检索到；
    # 旧令牌无 rid 字段，安全退回 VISITOR（仅公开文档）
    return CurrentUser(sub=str(data["sub"]), identity=identity,
                       name=data.get("name", ""),
                       identity_for_retrieval=str(data.get("rid") or "VISITOR"))


async def get_current_user(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> CurrentUser:
    raw = token
    if not raw and authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    return _resolve(raw)


async def require_admin(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> CurrentUser:
    """管理/审核接口：必须为管理员登录态。"""
    raw = token
    if not raw and authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    if not raw:
        raise HTTPException(status_code=401, detail="需要登录")
    user = _resolve(raw)
    if user.identity != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


async def require_reviewer(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> CurrentUser:
    """问答审核接口：管理员且命中审核员白名单。

    白名单配置的是管理员工号；令牌 sub 存的是管理员自增 id，
    因此命不中时按 id 回查工号再比对。QA_CACHE__REVIEWER_IDS 为空则不限制。
    """
    user = await require_admin(authorization=authorization, token=token)
    from app.config.settings import get_settings
    allowed = get_settings().qa_cache.reviewer_set()
    if not allowed or user.sub in allowed or user.name in allowed:
        return user
    emp_id = await _admin_emp_id(user.sub)
    if emp_id and emp_id in allowed:
        return user
    raise HTTPException(status_code=403, detail="当前账号不在审核员白名单")


async def _admin_emp_id(admin_id: str) -> str | None:
    """按管理员 id 取工号，用于旧令牌缺少工号时的白名单回查。"""
    from sqlalchemy import select

    from app.core.database import session_scope
    from app.models.entities import AdminInfo
    try:
        sub = int(admin_id)
    except (TypeError, ValueError):
        return None
    async with session_scope() as session:
        row = (await session.execute(
            select(AdminInfo).where(AdminInfo.id == sub,
                                    AdminInfo.deleted == 0))).scalar_one_or_none()
    return row.emp_id if row is not None else None
