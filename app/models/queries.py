"""通用查询辅助：软删除过滤、乐观锁更新封装。

红线：业务代码禁止手写 `deleted = 0` 散落条件，统一走 base_filter。
"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.orm.exc import StaleDataError

from app.core.exceptions import ConcurrentError
from app.models.entities import AuditMixin


def base_filter(entity_type):
    """所有 SELECT 的默认软删过滤。"""
    return select(entity_type).where(entity_type.deleted == 0)


def base_count(entity_type):
    """与 base_filter 配套的总量统计：分页 total 必须同样排除软删数据。

    注意：COUNT 若漏掉 deleted = 0，分页会虚高——页面上会出现「共 N 条、最后一页为空」。
    """
    return select(func.count()).select_from(entity_type).where(entity_type.deleted == 0)


async def update_with_lock(session, entity_type, pk_column_name: str, pk_value,
                           values: dict, expected_lock_version: int):
    """乐观锁更新：WHERE lock_version = :expect，命中 0 行即冲突。

    通过版本号比对防止并发覆盖（丢失更新）。
    """
    pk_col = getattr(entity_type, pk_column_name)
    stmt = (
        update(entity_type)
        .where(pk_col == pk_value)
        .where(entity_type.lock_version == expected_lock_version)
        .where(entity_type.deleted == 0)
        .values(**values, lock_version=expected_lock_version + 1)
    )
    result = await session.execute(stmt)
    if result.rowcount == 0:
        raise ConcurrentError("数据已被他人修改，请刷新后重试")
    return result.rowcount


async def soft_delete(session, entity_type, pk_column_name: str, pk_value,
                      expected_lock_version: int | None = None):
    """逻辑删除：UPDATE deleted=1（保留数据可审计、可恢复）。"""
    values = {"deleted": 1}
    if expected_lock_version is not None:
        return await update_with_lock(session, entity_type, pk_column_name, pk_value,
                                      values, expected_lock_version)
    pk_col = getattr(entity_type, pk_column_name)
    await session.execute(
        update(entity_type).where(pk_col == pk_value).values(**values)
    )
