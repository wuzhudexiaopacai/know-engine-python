"""文档领域事件。

发布方只发事件，监听器（document/service.py 内注册）负责推进流水线：
    DocumentUploaded  → 转换（CONVERTING → CONVERTED）
    DocumentConverted → 切片（CHUNKING → CHUNKED）
    DocumentChunked   → 向量化（→ VECTOR_STORED）
任一环失败置 FAILED，由 jobs.compensate 定时扫表重试（最终一致性）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.infra.event_bus import DomainEvent


@dataclass
class DocumentUploaded(DomainEvent):
    doc_id: int
    version_id: int
    # force=True 用于人工「重试转换」：跳过状态门槛，强制重跑
    force: bool = False


@dataclass
class DocumentConverted(DomainEvent):
    doc_id: int
    version_id: int
    # force=True 用于人工「重新切片」：允许对已 VECTOR_STORED 的版本重跑
    force: bool = False


@dataclass
class DocumentChunked(DomainEvent):
    doc_id: int
    version_id: int
