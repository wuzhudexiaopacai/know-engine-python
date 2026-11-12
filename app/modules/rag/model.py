"""RAG 管道共享数据模型。

用 dataclass 表达"检索 → 聚合 → 生成" 各环节传递的中间态，字段命名与前端 SSE
契约（camelCase）在 RagReference.to_dict() 处统一收口，内部保持 Python 命名风格。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RetrievedChunk:
    """一条检索命中（聚合/精排的输入单元，父子分段已回溯后的最终文本）。"""

    chunk_id: str
    text: str
    score: float                       # 召回相似度分（向量分或融合分）
    document_id: int
    doc_version: int = 0
    title: str = ""
    source: str = "vector"             # RetrievalSource 值
    metadata: dict = field(default_factory=dict)
    rerank_score: float | None = None  # 精排分，未精排为 None
    # 真实语义相似度（向量余弦/ES KNN 分），与内部融合名次分 score 区分，供引用展示
    similarity: float | None = None

    @property
    def parent_chunk_id(self) -> str | None:
        return self.metadata.get("parentChunkId")


@dataclass
class RagReference:
    """引用溯源（写入 chat_message.rag_references + SSE reference 事件）。"""

    document_id: str
    document_title: str | None
    chunk_id: str | None
    version: str | None
    chunk_content: str
    similarity_score: float | None
    retrieval_source: str
    rerank_score: float | None = None
    # 联网搜索来源的原始地址；知识库引用为 None，前端据此渲染为可点击外链
    url: str | None = None

    def to_dict(self) -> dict:
        """前端契约：camelCase，与 chat 页面溯源卡片一一对应。"""
        return {
            "documentId": self.document_id,
            "documentTitle": self.document_title,
            "chunkId": self.chunk_id,
            "version": self.version,
            "chunkContent": self.chunk_content,
            "similarityScore": self.similarity_score,
            "retrievalSource": self.retrieval_source,
            "rerankScore": self.rerank_score,
            "url": self.url,
        }


@dataclass
class IntentResult:
    """意图识别结果。"""

    related: bool = True
    intent: str = "其他"
    reasoning: str = ""
    entities: dict = field(default_factory=dict)

    @classmethod
    def from_llm(cls, raw: dict | None) -> "IntentResult":
        """LLM JSON 安全解析：字段缺失/类型异常一律回退到"相关+其他"，
        绝不让意图识别的抖动打断整条对话链路（可用性优先）。"""
        if not isinstance(raw, dict):
            return cls()
        entities = raw.get("entities")
        return cls(
            related=bool(raw.get("related", True)),
            intent=str(raw.get("intent", "其他")),
            reasoning=str(raw.get("reasoning", "")),
            entities=entities if isinstance(entities, dict) else {},
        )


@dataclass
class RouteResult:
    """查询路由结果。"""

    strategy: str = "knowledge_base"   # RetrievalStrategy 值
    reasoning: str = ""
    confidence: float = 0.0
