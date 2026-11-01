"""业务常量与枚举。

规范红线：状态/类型必须使用枚举，禁止散落的魔法字符串。
数据库中以 VARCHAR 存储枚举 .value（便于跨语言兼容）。
"""

from __future__ import annotations

from enum import StrEnum


class DocumentStatus(StrEnum):
    """文档生命周期状态机：INIT → UPLOADED → CONVERTING → CONVERTED → CHUNKED → VECTOR_STORED"""

    INIT = "INIT"
    UPLOADED = "UPLOADED"
    CONVERTING = "CONVERTING"
    CONVERTED = "CONVERTED"
    CHUNKING = "CHUNKING"
    CHUNKED = "CHUNKED"
    VECTOR_STORED = "VECTOR_STORED"
    FAILED = "FAILED"          # Python 版新增：显式失败态，供补偿任务识别重试


class KnowledgeBaseType(StrEnum):
    """知识库类型：文档语义检索 / 结构化数据查询。"""

    DOCUMENT_SEARCH = "DOCUMENT_SEARCH"
    DATA_QUERY = "DATA_QUERY"


class SplitType(StrEnum):
    """切片策略（对应 DocumentSplitterFactory 的入参）。"""

    LENGTH = "LENGTH"        # 按字数切分
    TITLE = "TITLE"          # Markdown 标题层级，父子分段
    REGEX = "REGEX"          # 正则切分
    SEPARATOR = "SEPARATOR"  # 分隔符切分
    SMART = "SMART"          # 智能：自动标题切分 + overlap=10%


class SegmentStatus(StrEnum):
    """分段状态：STORED 已入库 / VECTOR_STORED 已向量化。"""

    STORED = "STORED"
    VECTOR_STORED = "VECTOR_STORED"
    FAILED = "FAILED"


class MessageType(StrEnum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class KnowEngineIntent(StrEnum):
    """编程技术知识库的意图分类。"""

    BACKEND = "后端开发"
    FRONTEND = "前端与工程化"
    DATA_STORE = "数据库与中间件"
    ARCHITECTURE = "架构与分布式"
    DEVOPS = "运维部署与安全"
    OTHER = "其他"


class RetrievalStrategy(StrEnum):
    """查询路由的三种数据源策略。"""

    KNOWLEDGE_BASE = "knowledge_base"   # 向量/全文混合检索
    RELATIONAL_DB = "relational_db"      # Text2SQL
    GRAPH_DB = "graph_db"                # Text2Cypher（Python 版降级为关系库邻接查询）


class RetrievalSource(StrEnum):
    """检索结果来源标记（写入 rag_references）。"""

    VECTOR = "vector"
    FULLTEXT = "fulltext"
    HYBRID = "hybrid"
    SQL = "sql"
    GRAPH = "graph"
    QA_CACHE = "qa_cache"


class CacheStatus(StrEnum):
    """问答缓存条目状态机。"""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    DISABLED = "DISABLED"
    EXPIRED = "EXPIRED"


class DisableReason(StrEnum):
    MANUAL = "MANUAL"
    EXPIRED = "EXPIRED"
    KNOWLEDGE_CHANGED = "KNOWLEDGE_CHANGED"
    NEGATIVE_FEEDBACK = "NEGATIVE_FEEDBACK"
    LOW_HIT_RATE = "LOW_HIT_RATE"


class UserStatus(StrEnum):
    """C 端用户账号状态（管理员可禁用/启用）。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class AdminStatus(StrEnum):
    ON_JOB = "ON_JOB"
    OFF_JOB = "OFF_JOB"


# ---------------- SSE 事件协议常量（前后端契约，勿随意改动） ----------------
class SseEvent:
    """SSE 事件类型：与 chat.html 前端解析逻辑一一对应。"""

    PROGRESS = "progress"        # 管道阶段进度（意图识别/改写/路由/排序/生成）
    CONVERSATION = "conversation"  # 会话 ID（新会话首个事件，供前端同步地址栏）
    TOKEN = "token"              # LLM 逐字输出
    REFERENCE = "reference"      # RAG 引用溯源
    CARD = "card"                # 结构化卡片（方案/数据对比）
    ANSWER_MESSAGE = "answer_message"  # 答案消息 ID（评价按钮绑定）
    SUGGESTIONS = "suggestions"      # 回答后 follow-up 建议问题（done 前下发）
    DONE = "done"                # 流结束
    ERROR = "error"              # 管道级错误（含降级提示）


# ---------------- 元数据 Key 常量（对应 rag/constant） ----------------
class MetaKey:
    """分段 metadata JSON 的标准键名，切片 → 存储 → 检索全链路对齐。"""

    DOC_ID = "docId"
    DOC_VERSION = "docVersion"
    CHUNK_ID = "chunkId"
    PARENT_CHUNK_ID = "parentChunkId"
    BROTHER_CHUNK_ID = "brotherChunkId"
    BROTHER_INDEX = "brotherChunkIndex"
    BROTHER_TOTAL = "brotherChunkTotal"
    SKIP_EMBEDDING = "skipEmbedding"
    ACCESSIBLE_BY = "accessibleBy"
    TITLE = "title"
    ORDER = "order"
