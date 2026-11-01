"""初始化种子数据（幂等，可重复执行）。

预置演示账号，并附加编程技术域的演示文章/订阅数据
与 table_meta（Text2SQL Schema 感知）。生产环境通过 SEED_ENABLED=false 关闭。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, text

from app.core import database
from app.core.database import session_scope
from app.core.logging import get_logger
from app.models.entities import (
    KnowledgeSubscription, AdminInfo, TableMeta, TechArticle, UserInfo
)
from app.models.queries import base_filter
from app.modules.auth.password import hash_password

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


async def seed_if_needed() -> None:
    """启动时调用：账号不存在则创建（bcrypt 哈希，绝不保存明文）。"""
    await _drop_legacy_tables()
    async with session_scope() as session:
        # 清理旧汽车表的 table_meta 残留，避免其 DDL 继续注入 Text2SQL Schema
        await session.execute(delete(TableMeta).where(TableMeta.table_name.in_(_LEGACY_TABLES)))
        # ---- 测试用户：13800138000 / 123456 ----
        exists = (await session.execute(
            base_filter(UserInfo).where(UserInfo.phone == "13800138000"))).scalar_one_or_none()
        if exists is None:
            session.add(UserInfo(phone="13800138000", password=hash_password("123456"),
                                 name="测试用户", nickname="小测"))

        # ---- 测试管理员：TEST001 / 123456 ----
        exists = (await session.execute(
            base_filter(AdminInfo).where(AdminInfo.emp_id == "TEST001"))).scalar_one_or_none()
        if exists is None:
            session.add(AdminInfo(emp_id="TEST001", name="测试管理员",
                                  password=hash_password("123456"),
                                  job="知识库管理员", status="ON_JOB"))

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
