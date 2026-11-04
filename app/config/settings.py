"""全局配置中心。

所有配置项均可通过 环境变量 / .env 文件 覆盖，
命名规则：字段大写（如 LLM__API_KEY 覆盖 llm.api_key）。

工程细节：
- 敏感配置（API Key）绝不硬编码，MVP 阶段允许通过 .env 注入
- 中间件全部"可选降级"：Redis / ES 未配置时自动切换到内置轻量实现，
  保证开箱即用（dev），生产可平滑切换企业组件
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _env_lookup() -> dict[str, str]:
    """构造 ${VAR} 的解析来源：.env 优先，未配置时回落到进程环境变量。

    pydantic-settings 读取 .env 只用于字段解析，不会写入 os.environ，
    所以这里显式读一次，让配置里的 ${TAVILY_API_KEY} 在两种启动方式下都能展开。
    """
    values: dict[str, str] = {}
    env_path = Path(".env")
    if env_path.is_file():
        try:
            from dotenv import dotenv_values

            for k, v in dotenv_values(env_path).items():
                if v:                     # 空值视为未配置，让进程环境变量兜底
                    values[k] = v
        except Exception:                 # 读取失败不阻断启动
            pass
    for k, v in os.environ.items():
        values.setdefault(k, v)
    return values


def _expand_refs(value: str, lookup: dict[str, str]) -> str:
    return _ENV_REF.sub(lambda m: lookup.get(m.group(1) or m.group(2), m.group(0)), value)


class AppConfig(BaseSettings):
    """应用基础配置（环境变量前缀 APP__，如 APP__PORT 覆盖 port）。"""

    model_config = SettingsConfigDict(env_prefix="APP__", env_file=".env",
                                      env_file_encoding="utf-8", extra="ignore")

    name: str = "KnowEngine-Python"
    env: str = Field(default="dev", description="dev / prod")
    host: str = "0.0.0.0"
    port: int = 8009
    # 生产环境必须通过环境变量覆盖
    secret_key: str = Field(default="change-me-in-production", description="令牌签名密钥")
    token_ttl_seconds: int = 30 * 24 * 3600  # 令牌有效期 30 天（2592000 秒）
    cors_origins: list[str] = ["*"]


class DatabaseConfig(BaseSettings):
    """数据库配置：默认对接 docker-compose 的 MySQL 8；SQLite 仅作降级兑底。"""

    model_config = SettingsConfigDict(env_prefix="DB__", env_file=".env", extra="ignore")

    url: str = Field(
        default="mysql+aiomysql://know_engine:know_engine@localhost:3307/know_engine?charset=utf8mb4",
        description="SQLAlchemy 异步 DSN（宿主端口 3307，对应 docker-compose 的 3307:3306）",
    )
    # MySQL 不可达时是否自动降级为本地 SQLite（开发体验优先；生产设为 false）
    fallback_sqlite: bool = True
    sqlite_url: str = Field(
        default=f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'know_engine.db'}",
        description="降级库 DSN，仅 dev 使用",
    )
    pool_size: int = 20
    max_overflow: int = 10
    echo: bool = False


class RedisConfig(BaseSettings):
    """Redis 配置（docker-compose 默认 6379，密码与 compose 一致）。

    url 置空时 L2 缓存与分布式锁自动降级为进程内实现。
    """

    model_config = SettingsConfigDict(env_prefix="REDIS__", env_file=".env", extra="ignore")

    url: str = Field(default="redis://localhost:6379/0", description="空字符串表示不启用")


class SnowflakeConfig(BaseSettings):
    """雪花 ID 工作节点配置（env 前缀 SNOWFLAKE__）。

    多实例部署时每个实例的 worker_id 必须不同，否则会生成重复 ID。
    worker_id 置 -1 表示按主机名自动派生（容器/Pod 主机名唯一即可）。
    """

    model_config = SettingsConfigDict(env_prefix="SNOWFLAKE__", env_file=".env", extra="ignore")

    worker_id: int = Field(default=-1, description="-1 表示按主机名哈希自动派生；手动指定范围 0~1023")


class SchedulerConfig(BaseSettings):
    """定时补偿任务配置（env 前缀 SCHEDULER__）。

    多实例部署时各实例都可启动调度器，具体任务由 Redis 分布式锁抢占执行；
    如需彻底关闭某实例的调度能力（如单独部署 worker），置 enabled=false。
    """

    model_config = SettingsConfigDict(env_prefix="SCHEDULER__", env_file=".env", extra="ignore")

    enabled: bool = Field(default=True, description="是否启动定时补偿调度器")


class EventConfig(BaseSettings):
    """领域事件总线配置（env 前缀 EVENT__）。

    Redis 可用时事件写入 Stream（消费组保证一条事件只被一个实例处理，
    失败未 ACK 的消息由 XAUTOCLAIM 超时重投）；未配置 Redis 或关闭开关时退回进程内分发。
    """

    model_config = SettingsConfigDict(env_prefix="EVENT__", env_file=".env", extra="ignore")

    stream_enabled: bool = Field(default=True, description="Redis 可用时是否使用 Stream 作为事件队列")
    backlog_warn: int = Field(default=2000, description="Stream 积压条数告警阈值（调度器每分钟巡检）")
    pending_warn: int = Field(default=100, description="未 ACK 消息数告警阈值（持续偏高说明消费异常）")


class LlmConfig(BaseSettings):
    """LLM / Embedding 主模型配置（OpenAI 兼容协议，默认通义 DashScope）。"""

    model_config = SettingsConfigDict(env_prefix="LLM__", env_file=".env", extra="ignore")

    # 严禁硬编码真实密钥：通过 .env / 环境变量 LLM__API_KEY 注入；
    # 留空则 available=False，管道各环节走降级路径
    api_key: str = ""
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    chat_model: str = "qwen-plus"
    flash_model: str = "qwen-turbo"          # 标题生成等低延迟辅助任务
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1536
    timeout_seconds: float = 60.0
    max_retries: int = 3                      # 网络级重试次数（tenacity 指数退避）
    # 无 API Key 时的降级策略：llm 走"直通"（跳过改写/路由，用原始问题检索），
    # embedding 走本地哈希向量（仅保证可运行，语义质量有限）
    fallback_enabled: bool = True


class SpeechConfig(BaseSettings):
    """语音转写（STT）：走 DashScope OpenAI 兼容协议的多模态音频输入。

    api_key 留空时复用 LLM 的 api_key；未注册（403/404）由服务层降级为错误提示。
    """

    model_config = SettingsConfigDict(env_prefix="SPEECH__", env_file=".env", extra="ignore")

    api_key: str = Field(default="", description="空则复用 LLM__API_KEY")
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    asr_model: str = Field(default="qwen3-asr-flash", description="支持的音频转写模型")
    timeout_seconds: float = 60.0
    max_audio_bytes: int = 10 * 1024 * 1024   # 单段录音上限 10MB


class ImageConfig(BaseSettings):
    """文生图：DashScope 原生异步任务 API（提交 task + 轮询），与 Rerank 同域先例。

    api_key 留空时复用 LLM 的 api_key；模型无权限/未开通时由服务层降级为错误提示。
    """

    model_config = SettingsConfigDict(env_prefix="IMAGE__", env_file=".env", extra="ignore")

    api_key: str = Field(default="", description="空则复用 LLM__API_KEY")
    base_url: str = Field(
        default="https://dashscope.aliyuncs.com/api/v1",
        description="原生 API 前缀（text2image 提交与 tasks 轮询共用）",
    )
    model: str = "wan2.2-t2i-flash"
    default_size: str = "1024*1024"
    submit_timeout_seconds: float = 15.0
    poll_timeout_seconds: float = 120.0        # 轮询总时限

    # ---- 文档解析时的图片理解 ----
    # 视觉模型单独在后台"模型配置"里指定用途=图片理解，建议配轻量模型（如 qwen-vl-plus）
    parse_enabled: bool = True
    max_images: int = 50                       # 单文档最多处理图片数（扫描件可能整页成图）
    min_image_bytes: int = 1024                # 小于该字节数的图标/装饰图跳过
    max_image_bytes: int = 5 * 1024 * 1024     # 单图上限，超限只留占位链接不描述
    describe_concurrency: int = 3              # 并发描述数（控制视觉模型 QPS）
    describe_timeout_seconds: float = 20.0     # 单图描述超时，超时降级为位置文案


class RerankConfig(BaseSettings):
    """精排配置：优先 DashScope Rerank API，失败降级为 RRF 融合排序。"""

    model_config = SettingsConfigDict(env_prefix="RERANK__", env_file=".env", extra="ignore")

    enabled: bool = True
    base_url: str = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    model: str = "gte-rerank-v2"
    top_n: int = 5
    timeout_seconds: float = 10.0
    # 可选：阿里原生协议支持检索意图指令（qwen3-rerank 推荐携带），留空则不传
    instruct: str = ""


class VectorConfig(BaseSettings):
    """向量与混合检索配置（Elasticsearch 8.x，docker-compose 部署）。

    ES 不可用时自动降级：向量回存 MySQL + 进程内 numpy 余弦相似度，
    全文检索降级为 LIKE（保障 dev 无 ES 也能跑通全链路）。
    """

    model_config = SettingsConfigDict(env_prefix="VECTOR__", env_file=".env", extra="ignore")

    es_url: str = Field(default="http://localhost:9200", description="置空或连接失败时降级")
    es_username: str = ""
    es_password: str = ""
    # 注意：改索引名/分析器会使存量索引不可见，需先重建索引再切换
    index_name: str = "know-engine-chunks-v1"
    chunk_top_k: int = 30                     # 初召回数量（宽召回，交给精排/校验收敛）
    reranked_top_k: int = 8                   # 精排后进入 Prompt 的数量
    min_score: float = 0.35                   # 精排原始分参考值（仅日志记录；实际门控见 retrieval.py）
    # 单条上下文注入上限（字符）：父分段再长也只截断到该长度，避免单个引用挤爆上下文
    max_context_chars: int = 3000
    # Prompt 中参考资料总预算（字符）：超出后停止追加低排名分段
    max_prompt_context_chars: int = 12000


class GraphConfig(BaseSettings):
    """Neo4j 图数据库（Text2Cypher，docker-compose 部署）。"""

    model_config = SettingsConfigDict(env_prefix="NEO4J__", env_file=".env", extra="ignore")

    uri: str = Field(default="bolt://localhost:7687")
    username: str = "neo4j"
    password: str = "neo4j666"
    database: str = "neo4j"


class StorageConfig(BaseSettings):
    """对象存储：MinIO（cgr.dev/chainguard/minio 镜像，docker-compose 部署）。

    MinIO 不可达时自动降级为本地磁盘目录（接口抽象不变）。
    """

    model_config = SettingsConfigDict(env_prefix="STORAGE__", env_file=".env", extra="ignore")

    endpoint: str = Field(default="localhost:9000", description="host:port，不带协议")
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    bucket: str = "know-engine"
    secure: bool = False                       # compose 环境走 http
    public_base_url: str = "http://localhost:9000"  # 前端直读预签名 URL 前缀
    root: str = str(BASE_DIR / "data" / "files")     # 降级本地目录
    # 分片上传会话目录：多实例部署时必须指向各实例共享的目录（共享卷/NFS），
    # 留空则沿用 root/upload_sessions（单机默认行为）
    upload_root: str = ""
    # 大文件分片上传参数
    max_upload_bytes: int = 200 * 1024 * 1024   # 单文件上限 200MB
    chunk_size: int = 5 * 1024 * 1024            # 推荐分片大小 5MB
    upload_session_ttl_seconds: int = 24 * 3600  # 上传会话 24h 过期可断点续传


class ParseConfig(BaseSettings):
    """外部文档解析服务（MinerU）配置；未配置时降级为内置 pypdf/python-docx 解析。"""

    model_config = SettingsConfigDict(env_prefix="PARSE__", env_file=".env", extra="ignore")

    api_url: str = ""
    timeout_seconds: float = 300.0


class QaCacheConfig(BaseSettings):
    """审核式问答缓存配置。"""

    model_config = SettingsConfigDict(env_prefix="QA_CACHE__", env_file=".env", extra="ignore")

    enabled: bool = False
    index_name: str = "know-engine-qa-cache-v1"
    min_score: float = 0.95
    min_frequency: int = 3
    ttl_days: int = 90                       # 审核上线后的有效天数；<=0 表示永久有效
    reviewer_ids: str = Field(default="", description="逗号分隔的审核管理员账号（工号）")

    def reviewer_set(self) -> set[str]:
        return {x.strip() for x in self.reviewer_ids.split(",") if x.strip()}


class McpServerConfig(BaseModel):
    """单个 MCP 服务：stdio 启动本地进程，或 http 直连远程端点。

    stdio 示例：{"name":"search","transport":"stdio","command":"npx",
                "args":["-y","tavily-mcp"],"env":{"TAVILY_API_KEY":"${TAVILY_API_KEY}"}}
    http  示例：{"name":"tavily","transport":"http",
                "url":"https://mcp.tavily.com/mcp/?tavilyApiKey=${TAVILY_API_KEY}"}

    字符串字段支持 ${VAR} 引用环境变量，密钥只需放在环境变量里，不必写进配置。
    """

    name: str
    transport: str = "stdio"                 # stdio / http
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)


class McpConfig(BaseSettings):
    """MCP 客户端配置（MCP__ENABLED / MCP__SERVERS 等环境变量覆盖）。

    servers 为 JSON 数组，例如：
    MCP__SERVERS='[{"name":"tavily","transport":"http","url":"https://mcp.tavily.com/mcp/?tavilyApiKey=${TAVILY_API_KEY}"}]'
    """

    model_config = SettingsConfigDict(env_prefix="MCP__", env_file=".env", extra="ignore")

    enabled: bool = False
    servers: list[McpServerConfig] = Field(default_factory=list)
    timeout_seconds: float = 30.0            # 单次 list/call 的总超时
    max_rounds: int = 3                      # 工具调用循环上限，防止模型反复调用
    max_tools: int = 20                      # 暴露给模型的工具数上限（超了截断，控制 token）
    max_result_chars: int = 4000             # 单个工具结果注入上限，防撑爆上下文
    cache_ttl_seconds: int = 300             # tools/list 结果缓存时长
    max_servers: int = 6

    @model_validator(mode="after")
    def _expand_env_refs(self):
        """把 servers 里的 ${VAR} 展开为实际值（.env 优先，进程环境变量兜底）。"""
        lookup = _env_lookup()
        for srv in self.servers:
            srv.command = _expand_refs(srv.command, lookup)
            srv.args = [_expand_refs(a, lookup) for a in srv.args]
            srv.env = {k: _expand_refs(v, lookup) for k, v in srv.env.items()}
            srv.url = _expand_refs(srv.url, lookup)
            srv.headers = {k: _expand_refs(v, lookup) for k, v in srv.headers.items()}
        return self


class Settings(BaseSettings):
    """聚合根配置，通过 get_settings() 获取单例。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app: AppConfig = AppConfig()
    db: DatabaseConfig = DatabaseConfig()
    redis: RedisConfig = RedisConfig()
    snowflake: SnowflakeConfig = SnowflakeConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    event: EventConfig = EventConfig()
    llm: LlmConfig = LlmConfig()
    speech: SpeechConfig = SpeechConfig()
    image: ImageConfig = ImageConfig()
    rerank: RerankConfig = RerankConfig()
    vector: VectorConfig = VectorConfig()
    graph: GraphConfig = GraphConfig()
    storage: StorageConfig = StorageConfig()
    parse: ParseConfig = ParseConfig()
    qa_cache: QaCacheConfig = QaCacheConfig()
    mcp: McpConfig = McpConfig()


@lru_cache
def get_settings() -> Settings:
    """配置单例（进程内缓存，避免重复加载）。"""
    return Settings()
