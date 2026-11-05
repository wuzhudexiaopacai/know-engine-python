"""租户上下文与行级隔离。

SaaS 改造采用「共享库 + tenant_id 行级隔离」：
- 请求进入时由认证依赖写入当前租户 ID（contextvar）；
- 所有标记 `__tenant_scoped__ = True` 的实体在执行 SELECT/UPDATE/DELETE
  时自动追加 `tenant_id == 当前租户` 条件，业务代码无需逐条手写，
  从机制上避免漏加条件导致的跨租户越权；
- 新增实体在 flush 前自动补齐 tenant_id。

平台超管（跨租户）将上下文置为 None（不加过滤），租户侧账号则为自己的租户 ID。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

DEFAULT_TENANT_ID = 1     # 存量数据与访客默认归属的租户
PLATFORM_TENANT_ID = 0    # 平台自身（平台超管账号所在"租户"）

_current_tenant: ContextVar[int | None] = ContextVar("current_tenant_id", default=None)
_guards_installed = False


def get_current_tenant() -> int | None:
    """当前请求的租户 ID；None 表示不做租户过滤（仅平台超管）。"""
    return _current_tenant.get()


def set_current_tenant(tenant_id: int | None) -> None:
    _current_tenant.set(tenant_id)


@contextmanager
def tenant_scope(tenant_id: int | None) -> Iterator[None]:
    """临时切换租户上下文，退出时恢复，供后台任务显式切租户。"""
    token = _current_tenant.set(tenant_id)
    try:
        yield
    finally:
        _current_tenant.reset(token)


def install_tenant_guards() -> None:
    """注册全局会话钩子（幂等）。在数据库初始化后调用一次。"""
    global _guards_installed
    if _guards_installed:
        return
    _guards_installed = True

    from app.core.database import Base

    @event.listens_for(Session, "do_orm_execute")
    def _inject_tenant_filter(execute_state) -> None:
        tenant_id = get_current_tenant()
        if tenant_id is None:
            return
        if not (execute_state.is_select or execute_state.is_update or execute_state.is_delete):
            return
        for mapper in Base.registry.mappers:
            entity = mapper.class_
            if not getattr(entity, "__tenant_scoped__", False):
                continue
            # 用闭包变量而非默认参数捕获租户 ID：SQLAlchemy 默认把闭包变量纳入
            # 语句缓存键，不同租户才会各自命中正确的过滤条件；若用默认参数捕获，
            # 缓存键不含租户值，后续租户会复用首个租户的条件导致越权。
            tid = tenant_id
            execute_state.statement = execute_state.statement.options(
                with_loader_criteria(
                    entity,
                    lambda cls: cls.tenant_id == tid,
                    include_aliases=True,
                )
            )

    @event.listens_for(Session, "before_flush")
    def _stamp_tenant(session, _flush_context, _instances) -> None:
        tenant_id = get_current_tenant()
        if tenant_id is None:
            return
        for obj in session.new:
            if getattr(type(obj), "__tenant_scoped__", False) and getattr(obj, "tenant_id", None) is None:
                obj.tenant_id = tenant_id
