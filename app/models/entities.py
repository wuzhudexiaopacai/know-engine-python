"""ORM 实体定义（编程技术知识库）。

企业规范落地点：
- 公共字段抽象为 AuditMixin：created_at / updated_at / lock_version（乐观锁）/
  deleted（逻辑删除）；所有查询默认附加 deleted=0（全局软删过滤）
- 乐观锁：使用 SQLAlchemy `version_id_col` 原生支持，并发写冲突抛
  StaleDataError → 由 Service 层转换为 ConcurrentError
- JSON 字段跨库兼容：使用 JSON 类型 + 应用层序列化
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer,
    LargeBinary, String, Text, func
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

# 自增大整型主键的跨库变体：MySQL 用 BIGINT AUTO_INCREMENT，
# SQLite 只有 `INTEGER PRIMARY KEY` 才是 rowid 别名并自动递增，
# 故降级库下渲染为 INTEGER，避免“NOT NULL constraint failed: id”。
PkBigInt = BigInteger().with_variant(Integer, "sqlite")


class AuditMixin:
    """审计与并发控制公共字段。"""

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)   # 乐观锁
    deleted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)         # 逻辑删除 0/1


# ------------------------------------------------------------------
# 知识文档
# ------------------------------------------------------------------
class KnowledgeDocument(Base, AuditMixin):
    """知识文档表：一个文档可有多个版本，current_version_id 指向激活版本。"""

    __tablename__ = "knowledge_document"

    doc_id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    doc_title: Mapped[str] = mapped_column(String(1024), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    knowledge_base_type: Mapped[str] = mapped_column(String(32), default="DOCUMENT_SEARCH")
    accessible_by: Mapped[str | None] = mapped_column(String(1024))   # 可见范围（逗号分隔角色/部门）
    expire_date: Mapped[datetime | None] = mapped_column(DateTime)
    extension: Mapped[dict | None] = mapped_column(JSON)               # 扩展 JSON
    current_version_id: Mapped[int | None] = mapped_column(BigInteger)

    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (
        Index("idx_status_doc_id", "status", "doc_id"),
        Index("idx_created_at", "created_at"),
    )


class DocumentVersion(Base, AuditMixin):
    """文档版本表：每版本的原始/转换文件 URL、内容哈希（去重）、变更说明。"""

    __tablename__ = "knowledge_document_version"

    version_id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("knowledge_document.doc_id"), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    doc_url: Mapped[str | None] = mapped_column(String(2048))            # 原始文件
    converted_doc_url: Mapped[str | None] = mapped_column(String(2048))  # 转换后 Markdown
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)  # SHA-256 跨版本去重
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    upload_user: Mapped[str | None] = mapped_column(String(255))
    changelog: Mapped[str | None] = mapped_column(String(1024))

    document: Mapped[KnowledgeDocument] = relationship(back_populates="versions")

    __table_args__ = (
        Index("uk_doc_version", "doc_id", "version", unique=True),
    )


class KnowledgeSegment(Base, AuditMixin):
    """知识分段表：chunk_id 雪花 ID 全链路唯一，父子/兄弟关系由 metadata 描述。

    embedding 列存储 float32 原始字节（numpy tobytes），比 JSON 省 75% 空间，
    skip_embedding=1 的父分段不参与向量检索（命中子分段时回溯替换全文）。
    """

    __tablename__ = "knowledge_segment"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    chunk_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 父分段会整段落库（可达数十万字符），MySQL 的 TEXT 仅 64KB，必须用 LONGTEXT；
    # 其他方言保持 TEXT（SQLite 的 TEXT 无长度上限）。
    text: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT, "mysql"), nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    document_version: Mapped[int | None] = mapped_column(BigInteger, index=True)
    chunk_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str | None] = mapped_column(String(32), index=True)
    skip_embedding: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)   # float32 * dim
    embedding_id: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        Index("idx_document_id_chunk_order", "document_id", "chunk_order"),
        Index("idx_document_status_skip", "document_id", "status", "skip_embedding"),
    )


# ------------------------------------------------------------------
# 对话
# ------------------------------------------------------------------
class ChatConversation(Base, AuditMixin):
    __tablename__ = "chat_conversation"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="active")


class ChatMessage(Base, AuditMixin):
    """对话消息：rag_references 为 JSON 数组；helpful NULL 未评价 / 1 赞 / 0 踩。"""

    __tablename__ = "chat_message"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    transform_content: Mapped[str | None] = mapped_column(Text)   # 改写后的问题
    token_count: Mapped[int | None] = mapped_column(Integer)
    model_name: Mapped[str | None] = mapped_column(String(128))
    rag_references: Mapped[list | None] = mapped_column(JSON)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    cache_hit: Mapped[int] = mapped_column(Integer, default=0)
    helpful: Mapped[int | None] = mapped_column(Integer)          # NULL/1/0
    feedback_comment: Mapped[str | None] = mapped_column(String(1000))
    feedback_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("idx_create_time", "created_at"),
    )


class ChatConversationSummary(Base, AuditMixin):
    """会话长期滚动摘要：每达阈值把旧消息折叠进摘要，摘要 + 最近窗口 = 上下文。

    滚动策略：新摘要 = LLM(旧摘要 + 自上次摘要以来的消息)，只保留每个会话最新一条。
    """

    __tablename__ = "chat_conversation_summary"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    message_count: Mapped[int] = mapped_column(Integer, default=0)   # 折叠进摘要的消息条数


class UserPreference(Base, AuditMixin):
    """用户对话偏好：长期记忆开关、滑动窗口大小、摘要触发轮次。

    user_id 即 owner_key（identity:sub），按用户隔离；无记录时由 service 惰性创建默认行。
    """

    __tablename__ = "user_preference"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    long_term_memory_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    memory_window_size: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    summary_round_interval: Mapped[int] = mapped_column(Integer, default=8, nullable=False)


# ------------------------------------------------------------------
# 账号
# ------------------------------------------------------------------
class UserInfo(Base, AuditMixin):
    """C 端用户（手机号登录）。"""

    __tablename__ = "user_info"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    password: Mapped[str] = mapped_column(String(128), nullable=False)  # bcrypt 哈希
    name: Mapped[str | None] = mapped_column(String(64))
    nickname: Mapped[str | None] = mapped_column(String(128))
    avatar: Mapped[str | None] = mapped_column(String(512))
    motto: Mapped[str | None] = mapped_column(String(255))              # 个人座右铭
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")


class AdminInfo(Base, AuditMixin):
    """B 端管理员（工号登录，兼做知识库管理员/审核员）。"""

    __tablename__ = "admin_info"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    emp_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str | None] = mapped_column(String(64))
    password: Mapped[str | None] = mapped_column(String(128))
    job: Mapped[str | None] = mapped_column(String(128))
    avatar: Mapped[str | None] = mapped_column(String(512))
    motto: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="ON_JOB")


class LlmModel(Base, AuditMixin):
    """对话模型配置（管理员在后台维护，普通用户按 display_name 选择）。

    一行即一个"可被选中的模型"，自带 OpenAI 兼容协议的 base_url/api_key，
    因此本地 Ollama/vLLM 与云端阿里百炼、智谱、OpenAI 都可以用同一套流程接入。
    """

    __tablename__ = "llm_model"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    # 用途：逗号分隔的多值，如 'chat,vision'；一个模型可同时支持多种能力。
    # 单选场景（embedding/rerank 等专用模型）只有一个值。
    purpose: Mapped[str] = mapped_column(String(128), default="chat")
    provider: Mapped[str] = mapped_column(String(32), default="custom")   # aliyun/zhipu/bailian/openai/local/custom
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)  # 用户端展示名
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)    # 上游真实模型名
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key: Mapped[str] = mapped_column(String(512), default="")
    is_default: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    remark: Mapped[str | None] = mapped_column(String(512))
    # 是否支持图片输入（多模态）：仅当 chat 模型可识图时才把文档图片发给它，
    # 否则只传文字描述，避免服务端返回"不支持 image_url 类型"
    supports_vision: Mapped[int] = mapped_column(Integer, default=0)


class ModelRoleBinding(Base, AuditMixin):
    """模型用途绑定：一个系统用途（角色）绑定一个已配置模型。

    role 为主键（chat/vision/speech/embedding/rerank/image_gen/memory），
    model_id 为空表示未绑定（由服务层按用途默认模型或环境变量兜底）。
    与 llm_model.purpose 的区别：purpose 描述「这个模型能干什么」，
    本表描述「系统这个位置当前用哪个模型」，两者解耦后同一模型可被多处复用。
    """

    __tablename__ = "model_role_binding"

    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    model_id: Mapped[int | None] = mapped_column(BigInteger)


# ------------------------------------------------------------------
# Text2SQL 结构化数据（示例业务域：编程技术知识库）
# ------------------------------------------------------------------
class TableMeta(Base, AuditMixin):
    """表结构元数据：驱动 Text2SQL 的 Schema 感知。"""

    __tablename__ = "table_meta"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(512))
    create_sql: Mapped[str | None] = mapped_column(Text)
    columns_info: Mapped[str | None] = mapped_column(Text)   # JSON 文本
    version_id: Mapped[int | None] = mapped_column(BigInteger, index=True)


class TechArticle(Base, AuditMixin):
    """技术文章元数据表（Text2SQL 演示数据源之一）。"""

    __tablename__ = "tech_article"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    article_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str | None] = mapped_column(String(256), index=True)
    language: Mapped[str | None] = mapped_column(String(64), index=True)
    framework: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str | None] = mapped_column(String(64))
    publish_date: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    read_count: Mapped[int] = mapped_column(Integer, default=0)
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    author: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str | None] = mapped_column(String(32), default="已发布")
    description: Mapped[str | None] = mapped_column(Text)


class KnowledgeSubscription(Base, AuditMixin):
    """知识订阅表（Text2SQL 演示数据源之二）。"""

    __tablename__ = "knowledge_subscription"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    subscribe_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    topic: Mapped[str | None] = mapped_column(String(128), index=True)
    difficulty: Mapped[str | None] = mapped_column(String(32))
    frequency: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str | None] = mapped_column(String(32), default="生效中", index=True)
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    remark: Mapped[str | None] = mapped_column(Text)


# ------------------------------------------------------------------
# 问答缓存
# ------------------------------------------------------------------
class QaCacheEntry(Base, AuditMixin):
    """审核式问答缓存条目（状态机：PENDING→APPROVED→ACTIVE→DISABLED/EXPIRED）。

    embedding 列仅编码"标准问题"，答案以 MySQL/本库为唯一事实源；
    fingerprint 防重复入库，question_key 精确去重。
    """

    __tablename__ = "qa_cache_entry"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    question_key: Mapped[str | None] = mapped_column(String(32), index=True)
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    sources: Mapped[list] = mapped_column(JSON, nullable=False)     # 来源问答快照
    frequency: Mapped[int] = mapped_column(Integer, default=1)
    quality: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    reviewer: Mapped[str | None] = mapped_column(String(128))
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    index_error: Mapped[str | None] = mapped_column(String(256))
    hits: Mapped[int] = mapped_column(Integer, default=0)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime)
    disable_reason: Mapped[str | None] = mapped_column(String(32))
    disable_detail: Mapped[str | None] = mapped_column(String(1000))
    dependencies: Mapped[list | None] = mapped_column(JSON)          # 引用的文档版本
    vector_deleted: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)

    __table_args__ = (
        Index("idx_qa_cache_status_created", "status", "created_at"),
    )


class QaCacheLookup(Base, AuditMixin):
    """缓存查询记录：命中占比统计的分母来源（低命中率自动准出用）。"""

    __tablename__ = "qa_cache_lookup"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    entry_id: Mapped[str | None] = mapped_column(String(36), index=True)  # 命中条目；未命中为空
    hit: Mapped[bool] = mapped_column(Boolean, default=False)
    queried_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
