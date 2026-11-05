"""初始化种子数据（幂等，可重复执行）。

SaaS 改造后预置：
- 平台租户（id=0, code=platform）与默认租户（id=1, code=default）
- 权限字典与内置角色（由 rbac.ensure_rbac_seed 写入）
- 平台超管 / 租户管理员 / 普通成员演示账号
- 编程技术域演示文章、订阅与 table_meta（Text2SQL Schema 感知）
生产环境通过 SEED_ENABLED=false 关闭。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, text

from app.core import database
from app.core.database import session_scope
from app.core.logging import get_logger
from app.models.entities import (
    Account, AccountRole, KnowledgeSubscription, Role, TableMeta, TechArticle, Tenant,
)
from app.models.queries import base_filter
from app.modules.auth.password import hash_password
from app.modules.rbac import service as rbac
from app.modules.rbac.constants import RoleCode
from app.core.tenant import PLATFORM_TENANT_ID as PLATFORM

logger = get_logger("app.seed")

# 迁移清理：旧汽车演示表已废弃，启动时幂等移除（MySQL/SQLite 均支持 IF EXISTS）
_LEGACY_TABLES = ("car_info", "car_order")


async def _drop_legacy_tables() -> None:
    """删除已废弃的汽车演示表，使存量库收敛到新域。"""
    if database.engine is None:
        return
    async with database.engine.begin() as conn:
        for t in _LEGACY_TABLES:
            await conn.execute(text(f"DROP TABLE IF EXISTS {t}"))


async def _ensure_tenant(session, tenant_id: int, code: str, name: str) -> Tenant:
    tenant = (await session.execute(
        base_filter(Tenant).where(Tenant.code == code))).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(id=tenant_id, code=code, name=name, status="ACTIVE", plan="STANDARD")
        session.add(tenant)
        await session.flush()
    return tenant


async def _ensure_account(session, tenant_id: int, login_id: str, login_type: str,
                          name: str, nickname: str | None, role_code: str,
                          job: str | None = None) -> None:
    account = (await session.execute(
        base_filter(Account).where(Account.tenant_id == tenant_id,
                                   Account.login_type == login_type,
                                   Account.login_id == login_id))).scalar_one_or_none()
    if account is None:
        account = Account(tenant_id=tenant_id, login_id=login_id, login_type=login_type,
                          password=hash_password("123456"), name=name, nickname=nickname,
                          job=job, status="ACTIVE")
        session.add(account)
        await session.flush()
    role = (await session.execute(
        base_filter(Role).where(Role.tenant_id == 0, Role.code == role_code))).scalar_one_or_none()
    if role is None:
        return
    bound = (await session.execute(
        base_filter(AccountRole).where(AccountRole.account_id == account.id,
                                       AccountRole.role_id == role.id))).scalar_one_or_none()
    if bound is None:
        session.add(AccountRole(account_id=account.id, role_id=role.id))


async def seed_if_needed() -> None:
    """启动时调用：先初始化 RBAC 字典，再补齐租户/账号/演示数据。"""
    await _drop_legacy_tables()
    await rbac.ensure_rbac_seed()
    async with session_scope() as session:
        # 清理旧汽车表的 table_meta 残留，避免其 DDL 继续注入 Text2SQL Schema
        await session.execute(delete(TableMeta).where(TableMeta.table_name.in_(_LEGACY_TABLES)))

        # ---- 平台租户（id=0）与默认租户（id=1，存量数据归属） ----
        await _ensure_tenant(session, PLATFORM, "platform", "平台")
        await _ensure_tenant(session, 1, "default", "默认租户")

        # ---- 演示账号（统一 account 表 + RBAC 角色） ----
        await _ensure_account(session, PLATFORM, "PLATFORM001", "EMP", "平台超管", None,
                              RoleCode.PLATFORM_ADMIN)
        await _ensure_account(session, 1, "TEST001", "EMP", "测试管理员", None,
                              RoleCode.TENANT_ADMIN, job="租户管理员")
        await _ensure_account(session, 1, "13800138000", "PHONE", "测试用户", "小测",
                              RoleCode.MEMBER)

        # ---- 演示技术文章 ----
        article_exists = (await session.execute(
            base_filter(TechArticle).where(TechArticle.deleted == 0))).scalars().first()
        if article_exists is None:
            session.add_all([
                TechArticle(article_id="ART-REDIS-PERSIST", title="Redis 持久化 RDB 与 AOF 对比",
                            language="通用", framework="", category="数据库与中间件",
                            publish_date=datetime(2025, 3, 12), read_count=8213, like_count=356,
                            author="胡图图", status="已发布",
                            description="对比 RDB 快照与 AOF 日志的取舍、混合持久化配置与恢复速度"),
                TechArticle(article_id="ART-K8S-SCHEDULE", title="Kubernetes Pod 调度原理",
                            language="通用", framework="", category="运维部署与安全",
                            publish_date=datetime(2025, 6, 20), read_count=5478, like_count=289,
                            author="云原生小组", status="已发布",
                            description="打分与抢占两阶段、亲和性与污点容忍、调度扩展器"),
            ])

        # ---- 演示知识订阅 ----
        sub_exists = (await session.execute(
            base_filter(KnowledgeSubscription).where(
                KnowledgeSubscription.subscribe_id == "SUB-DEMO-0001"))).scalar_one_or_none()
        if sub_exists is None:
            session.add(KnowledgeSubscription(
                subscribe_id="SUB-DEMO-0001", user_id="13800138000", topic="分布式事务",
                difficulty="进阶", frequency="每周", status="生效中",
                create_time=datetime(2025, 7, 1),
                remark="演示订阅：2PC/TCC/Saga 主题推送"))

        # ---- Text2SQL Schema 元数据 ----
        for table_name, ddl, desc in _TABLE_METAS:
            exists = (await session.execute(
                base_filter(TableMeta).where(TableMeta.table_name == table_name))).scalar_one_or_none()
            if exists is None:
                session.add(TableMeta(table_name=table_name, create_sql=ddl, description=desc,
                                      columns_info=""))
    logger.info("seed_done")


_TABLE_METAS = [
    ("tech_article",
     "CREATE TABLE tech_article (article_id TEXT, title TEXT, language TEXT, framework TEXT, "
     "category TEXT, publish_date DATETIME, read_count INT, like_count INT, author TEXT, "
     "status TEXT, description TEXT)",
     "技术文章表：标题、语言/框架、分类、发布时间、阅读量、点赞数、作者"),
    ("knowledge_subscription",
     "CREATE TABLE knowledge_subscription (subscribe_id TEXT, user_id TEXT, topic TEXT, "
     "difficulty TEXT, frequency TEXT, status TEXT, create_time DATETIME, remark TEXT)",
     "知识订阅表：用户订阅的技术主题、难度、推送频率与生效状态"),
]
