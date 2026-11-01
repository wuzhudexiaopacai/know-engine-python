"""文档模块的 Pydantic 数据模型（请求 / 响应 schema）。

红线：路由层只收发 schema，禁止把 ORM Entity 直接返回给前端
（防止懒加载异常与内部字段泄露）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# ---------------- 分片上传 ----------------
class ChunkInitRequest(BaseModel):
    """初始化上传会话：客户端计算整文件 MD5（秒传依据）。"""

    file_name: str = Field(min_length=1, max_length=200)
    file_size: int = Field(gt=0, description="字节数，服务端二次校验上限")
    file_md5: str = Field(min_length=32, max_length=32, description="整文件 MD5（hex）")
    chunk_size: int = Field(default=0, description="0 表示采用服务端推荐值")
    doc_id: int | None = Field(default=None, description="指定则为已有文档上传新版本")
    changelog: str | None = Field(default=None, max_length=1024)


class ChunkInitResponse(BaseModel):
    upload_id: str | None = None
    uploaded_chunks: list[int] = []      # 断点续传：已存在的分片序号
    chunk_size: int
    instant_hit: bool = False            # 秒传命中
    doc_id: int | None = None
    version_id: int | None = None


class ChunkFinalizeRequest(BaseModel):
    upload_id: str
    # 切片配置（上传表单参数）
    split_type: str = Field(default="SMART", pattern="^(LENGTH|TITLE|REGEX|SEPARATOR|SMART)$")
    chunk_size: int = Field(default=800, ge=50, le=4000)
    overlap: int = Field(default=0, ge=0, le=1000)
    title_level: int = Field(default=0, ge=0, le=6, description="TITLE 模式限定标题层级，0=全部")
    regex: str | None = None
    separator: str | None = None
    knowledge_base_type: str = Field(default="DOCUMENT_SEARCH",
                                     pattern="^(DOCUMENT_SEARCH|DATA_QUERY)$")
    accessible_by: str | None = Field(default=None, max_length=1024)
    expire_date: datetime | None = None
    description: str | None = Field(default=None, max_length=512)


# ---------------- 文档管理 ----------------
class DocumentVO(BaseModel):
    """列表/详情视图对象。"""

    doc_id: int
    doc_title: str
    status: str
    knowledge_base_type: str
    description: str | None = None
    accessible_by: str | None = None
    expire_date: datetime | None = None
    current_version_id: int | None = None
    current_version: str | None = None
    segment_count: int = 0
    lock_version: int = 0          # 乐观锁版本，供前端切换/删除回传
    # 扩展信息：切片配置 / 文件类型 / 各版本流水线阶段轨迹与失败原因（前端流程可视化）
    extension: dict | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DocumentVersionVO(BaseModel):
    version_id: int
    doc_id: int
    version: str
    status: str
    changelog: str | None = None
    upload_user: str | None = None
    doc_url: str | None = None
    converted_doc_url: str | None = None   # 转换产物（预览/流程透明用）
    content_hash: str | None = None        # 内容 SHA-256（跨版本去重）
    lock_version: int = 0          # 版本行乐观锁
    created_at: datetime

    model_config = {"from_attributes": True}


class SegmentVO(BaseModel):
    chunk_id: str
    text: str
    chunk_order: int
    status: str | None = None
    skip_embedding: int = 0
    parent_chunk_id: str | None = None
    brother_chunk_id: str | None = None
    # 向量化可见性：维度 / 模长 / 前若干维数值（前端做效果示意）
    embedding_dim: int = 0
    vector_norm: float | None = None
    vector_preview: list[float] = []
    metadata: dict | None = Field(default=None, alias="metadata_")

    model_config = {"from_attributes": True, "populate_by_name": True}


class SegmentListVO(BaseModel):
    """切片与向量化结果（分页），供前端查看"分片效果/向量效果"。"""

    total: int
    page: int
    page_size: int
    embedded_count: int          # 已写入向量的分段数
    skipped_count: int           # 跳过向量化的分段数（父分段）
    vector_dim: int              # 向量维度
    records: list[SegmentVO]


class SplitConfigUpdate(BaseModel):
    """重新切片请求（改参数后对当前版本重跑 CHUNKING→VECTOR_STORED）。"""

    split_type: str = Field(pattern="^(LENGTH|TITLE|REGEX|SEPARATOR|SMART)$")
    chunk_size: int = Field(default=800, ge=50, le=4000)
    overlap: int = Field(default=0, ge=0, le=1000)
    title_level: int = Field(default=0, ge=0, le=6)
    regex: str | None = None
    separator: str | None = None
