"""文档处理流水线服务（事件监听 + 状态机推进 + 阶段留痕）。

事件驱动的三阶段流水线：
    DocumentUploaded  → _on_uploaded  （转换）
    DocumentConverted → _on_converted （切片）
    DocumentChunked   → _on_chunked   （向量化入库）

弹性设计：
- 每阶段独立 try/except：失败置 FAILED 并把错误/失败阶段写入 extension 的阶段轨迹，
  绝不上抛（补偿任务 jobs.compensate 扫版本状态重试 → 最终一致性）
- 三阶段全部持分布式锁（wait_time=0）：同一文档并发处理直接拒绝，防重复转换/切片/向量化
- 阶段轨迹存 document.extension.versions[versionId]，前端可查看转换→切片→向量化全过程
- extension 一律增量合并，绝不整块覆盖（旧实现覆盖会丢失切片配置与文件类型）
- 文档级状态只在处理"当前激活版本"时更新；旧版本重试失败不会把可用文档标成失败
"""

from __future__ import annotations

import re
import traceback
from datetime import datetime

import numpy as np

from sqlalchemy import select, update

from app.core.database import session_scope
from app.core.logging import get_logger
from app.infra.event_bus import get_event_bus
from app.infra.file_storage import get_file_store
from app.infra.lock import get_lock
from app.infra.snowflake import next_id_str
from app.infra.vector_store import ChunkRecord, get_vector_store
from app.models.constants import DocumentStatus, MetaKey, SegmentStatus, SplitType
from app.models.entities import DocumentVersion, KnowledgeDocument, KnowledgeSegment
from app.modules.document.converter import DocumentConverter, content_hash
from app.modules.document.events import (
    DocumentChunked, DocumentConverted, DocumentUploaded,
)
from app.modules.document.splitters import Chunk, ExcelSplitter, TableMode, create_splitter

logger = get_logger("app.document.pipeline")

_converter = DocumentConverter()

# 转换产物里的图片占位符（未被描述（无视觉模型/超限）时需从 Markdown 中清理）
_IMG_MARKER_RE = re.compile(r"!\[图片\]\(__KE_IMG_\d+__\)")

# 阶段轨迹状态（前端按此渲染时间线）
_STAGE_STARTED = "STARTED"
_STAGE_SUCCEEDED = "SUCCEEDED"
_STAGE_FAILED = "FAILED"


# ------------------------------------------------------------------
# 状态推进与阶段留痕
# ------------------------------------------------------------------
async def _set_status(doc_id: int, version_id: int, doc_status: str | None,
                      ver_status: str | None) -> None:
    """推进文档/版本状态。

    文档级状态仅在处理"当前激活版本"时同步；且当前版本失败但仍有可用历史版本时，
    文档状态保持 VECTOR_STORED（不把可用文档整体标红）。
    """
    async with session_scope() as session:
        if ver_status:
            await session.execute(
                update(DocumentVersion)
                .where(DocumentVersion.version_id == version_id)
                .values(status=ver_status))
        if doc_status:
            current = (await session.execute(
                select(KnowledgeDocument.current_version_id)
                .where(KnowledgeDocument.doc_id == doc_id,
                       KnowledgeDocument.deleted == 0))).scalar_one_or_none()
            if current == version_id:
                effective = doc_status
                if doc_status == DocumentStatus.FAILED:
                    ready = (await session.execute(
                        select(DocumentVersion.version_id).where(
                            DocumentVersion.doc_id == doc_id,
                            DocumentVersion.deleted == 0,
                            DocumentVersion.version_id != version_id,
                            DocumentVersion.status == DocumentStatus.VECTOR_STORED)
                        .limit(1))).scalar_one_or_none()
                    if ready is not None:
                        effective = DocumentStatus.VECTOR_STORED
                await session.execute(
                    update(KnowledgeDocument)
                    .where(KnowledgeDocument.doc_id == doc_id,
                           KnowledgeDocument.deleted == 0)
                    .values(status=effective))


async def _record_stage(doc_id: int, version_id: int, stage: str,
                        status: str, detail: str = "") -> None:
    """在 extension.versions[versionId] 追加阶段轨迹（增量合并，不覆盖配置）。"""
    async with session_scope() as session:
        doc = (await session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.doc_id == doc_id,
                KnowledgeDocument.deleted == 0))).scalar_one_or_none()
        if doc is None:
            return
        ext = dict(doc.extension or {})
        versions = dict(ext.get("versions") or {})
        entry = dict(versions.get(str(version_id)) or {})
        stages = list(entry.get("stages") or [])
        stages.append({"stage": stage, "status": status,
                       "at": datetime.now().isoformat(timespec="seconds"),
                       "detail": (detail or "")[:500]})
        entry["stages"] = stages[-20:]
        if status == _STAGE_FAILED:
            entry["lastError"] = (detail or "处理失败")[:1000]
            entry["failedStage"] = stage
            entry["failedAt"] = datetime.now().isoformat(timespec="seconds")
        elif status == _STAGE_SUCCEEDED:
            entry.pop("lastError", None)
            entry.pop("failedStage", None)
            entry.pop("failedAt", None)
            entry["attempts"] = 0
        versions[str(version_id)] = entry
        ext["versions"] = versions
        doc.extension = ext
        session.add(doc)


async def _load_pipeline_ctx(doc_id: int, version_id: int) -> dict | None:
    """读取流水线所需上下文；文档/版本不存在或已删除时返回 None（流水线静默退出）。"""
    async with session_scope() as session:
        doc = (await session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.doc_id == doc_id,
                KnowledgeDocument.deleted == 0))).scalar_one_or_none()
        if doc is None:
            return None
        ver = (await session.execute(
            select(DocumentVersion).where(
                DocumentVersion.version_id == version_id,
                DocumentVersion.doc_id == doc_id,
                DocumentVersion.deleted == 0))).scalar_one_or_none()
        if ver is None:
            return None
        ext = doc.extension or {}
        return {
            "status": doc.status, "version_status": ver.status,
            "file_type": ext.get("fileType", "text"),
            "split_config": ext.get("splitConfig", {"split_type": "SMART", "chunk_size": 800}),
            "doc_title": doc.doc_title, "accessible_by": doc.accessible_by,
            "tenant_id": doc.tenant_id, "expire_date": doc.expire_date,
            "doc_url": ver.doc_url, "converted_doc_url": ver.converted_doc_url,
            "content_hash": ver.content_hash,
            "kb_type": doc.knowledge_base_type,
            # Excel 直通无转换产物，图片说明由转换阶段写入 extension，切片时补成分段
            "image_notes": (ext.get("imageNotes") or {}).get(str(version_id), []),
        }


# ------------------------------------------------------------------
# 阶段一：转换
# ------------------------------------------------------------------
async def _on_uploaded(event: DocumentUploaded) -> None:
    ctx = await _load_pipeline_ctx(event.doc_id, event.version_id)
    if ctx is None:
        return
    # 幂等防护：仅 UPLOADED/FAILED/CONVERTING 中断态才进入转换（force 供人工重试）
    if not event.force and ctx["version_status"] not in (
            DocumentStatus.UPLOADED.value, DocumentStatus.FAILED.value,
            DocumentStatus.CONVERTING.value):
        return
    lock = get_lock()
    try:
        async with lock.hold("document-convert", str(event.doc_id), wait_time=0):
            await _convert_pipeline(event.doc_id, event.version_id)
    except Exception as e:
        if "重复提交" in str(e):
            logger.info("convert_skip_locked", doc_id=event.doc_id)
            return
        logger.error("stage_convert_blocked", doc_id=event.doc_id, error=str(e))


async def _convert_pipeline(doc_id: int, version_id: int) -> None:
    ctx = await _load_pipeline_ctx(doc_id, version_id)
    if ctx is None or not ctx["doc_url"]:
        return
    await _set_status(doc_id, version_id, DocumentStatus.CONVERTING, DocumentStatus.CONVERTING)
    await _record_stage(doc_id, version_id, "convert", _STAGE_STARTED,
                        f"文件类型 {ctx['file_type']}")
    try:
        src = await get_file_store().get(ctx["doc_url"])
        file_type = ctx["file_type"]

        result = await _converter.convert(src, file_type)
        described, image_note = await _describe_images(result.images)
        out_url = None
        if result.path is not None:
            _rewrite_markdown_images(result.path, described)
            # 先算哈希再入库：LocalFileStore.put 为 move 语义（搬走临时文件），
            # MinioFileStore.put 为 copy 语义；统一顺序避免移动后读源文件报 FileNotFoundError
            c_hash = content_hash(result.path)
            out_url = await get_file_store().put("converted", result.path.name, result.path)
            result.path.unlink(missing_ok=True)
        else:
            c_hash = content_hash(src)
            # Excel/CSV 直通没有 Markdown 产物，图片说明暂存 extension，切片时补成分段
            if described:
                await _save_image_notes(doc_id, version_id, described)

        async with session_scope() as session:
            values: dict = {"status": DocumentStatus.CONVERTED}
            if out_url:
                values["converted_doc_url"] = out_url
            if c_hash:
                values["content_hash"] = c_hash
            await session.execute(update(DocumentVersion)
                                  .where(DocumentVersion.version_id == version_id)
                                  .values(**values))
        # 重跑转换会生成新的转换产物，清理上一轮的旧产物，避免存储泄漏
        old_converted = ctx["converted_doc_url"]
        if old_converted and old_converted != out_url:
            try:
                await get_file_store().delete(old_converted)
            except Exception as cleanup_error:  # pragma: no cover - 清理失败不影响主流程
                logger.warning("converted_cleanup_failed", key=old_converted,
                               error=str(cleanup_error))
        await _set_status(doc_id, version_id, DocumentStatus.CONVERTED, DocumentStatus.CONVERTED)
        detail = f"产出 {out_url}" if out_url else "Excel/CSV 直通，无需转换"
        if described:
            detail += f"；图片 {len(described)} 张，描述 {sum(d.described for d in described)} 张"
        if image_note:
            detail += f"；{image_note}"
        await _record_stage(doc_id, version_id, "convert", _STAGE_SUCCEEDED, detail)
        logger.info("stage_converted", doc_id=doc_id, version_id=version_id)
        get_event_bus().publish(DocumentConverted(doc_id=doc_id, version_id=version_id))
    except Exception as e:
        logger.error("stage_convert_failed", doc_id=doc_id, error=str(e),
                     tb=traceback.format_exc()[-1500:])
        await _set_status(doc_id, version_id, DocumentStatus.FAILED, DocumentStatus.FAILED)
        await _record_stage(doc_id, version_id, "convert", _STAGE_FAILED, str(e))


async def _describe_images(images) -> tuple[list, str]:
    """上传文档内图片并生成描述；任何异常都降级为空结果，不阻断转换。

    返回 (图片结果列表, 降级原因)，原因写入阶段详情便于后台排查
    （例如「模型不支持图片输入」）。
    """
    if not images:
        return [], ""
    from app.config.settings import get_settings

    if not get_settings().image.parse_enabled:
        return [], ""
    from app.services.image_describer import prepare_images

    try:
        outcome = await prepare_images(images)
        return list(outcome), outcome.note
    except Exception as e:
        logger.warning("image_describe_batch_failed", error=str(e))
        return [], ""


def _rewrite_markdown_images(path, described: list) -> None:
    """把图片占位符替换为「描述 + 回读 URL」；未处理的占位符一并清理。"""
    text = path.read_text(encoding="utf-8")
    for item in described:
        text = text.replace(f"![图片]({item.marker})", f"![{item.alt}]({item.url})")
    text = _IMG_MARKER_RE.sub("", text)
    path.write_text(text, encoding="utf-8")


async def _save_image_notes(doc_id: int, version_id: int, described: list) -> None:
    """Excel 直通路径的图片说明写入 extension（切片阶段再补成独立分段）。"""
    notes = [{
        "label": d.label,
        "text": d.description or f"图片（{d.label}）",
        "url": d.url,
    } for d in described]
    async with session_scope() as session:
        doc = (await session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.doc_id == doc_id,
                KnowledgeDocument.deleted == 0))).scalar_one_or_none()
        if doc is None:
            return
        ext = dict(doc.extension or {})
        notes_map = dict(ext.get("imageNotes") or {})
        notes_map[str(version_id)] = notes
        ext["imageNotes"] = notes_map
        doc.extension = ext
        session.add(doc)


def _image_note_chunks(ctx: dict, base_meta: dict, start_order: int) -> list[Chunk]:
    """把 extension 中的图片说明补成独立分段（Excel 无转换产物时使用）。"""
    chunks: list[Chunk] = []
    for note in ctx.get("image_notes") or []:
        text = f"图片说明（{note.get('label', '')}）：{note.get('text', '')}"
        if note.get("url"):
            text += f"\n![图片]({note['url']})"
        chunks.append(Chunk(text, {
            **base_meta,
            MetaKey.CHUNK_ID: next_id_str(),
            MetaKey.ORDER: start_order + len(chunks),
        }))
    return chunks


# ------------------------------------------------------------------
# 阶段二：切片（分布式锁互斥）
# ------------------------------------------------------------------
async def _on_converted(event: DocumentConverted) -> None:
    lock = get_lock()
    try:
        async with lock.hold("document-split", str(event.doc_id), wait_time=0):
            await _split_pipeline(event.doc_id, event.version_id, force=event.force)
    except Exception as e:
        # 锁冲突=已有处理在途（正常现象，warning 即可）；其余错误进 FAILED
        if "重复提交" in str(e):
            logger.info("split_skip_locked", doc_id=event.doc_id)
            return
        logger.error("stage_split_blocked", doc_id=event.doc_id, error=str(e))


async def _split_pipeline(doc_id: int, version_id: int, force: bool = False) -> None:
    ctx = await _load_pipeline_ctx(doc_id, version_id)
    if ctx is None:
        return
    # force=True 供「重新切片」：即使已 VECTOR_STORED 也允许重跑
    if not force and ctx["version_status"] == DocumentStatus.VECTOR_STORED:
        return
    await _set_status(doc_id, version_id, DocumentStatus.CHUNKING, DocumentStatus.CHUNKING)
    await _record_stage(doc_id, version_id, "split", _STAGE_STARTED,
                        f"策略 {ctx['split_config'].get('split_type', 'SMART')}")
    try:
        conf = ctx["split_config"]
        file_type = ctx["file_type"]

        # 1. 取源文本（Excel 直通原始文件；其余用转换产物）
        if file_type in ("excel", "csv"):
            src_path = await get_file_store().get(ctx["doc_url"])
            content_text = None
        else:
            md_url = ctx["converted_doc_url"] or ctx["doc_url"]
            src_path = await get_file_store().get(md_url)
            content_text = src_path.read_text(encoding="utf-8")

        # 2. 清理旧分段（重新切片场景：先删旧分段 + 向量记录）
        await _purge_segments(doc_id, version_id)
        # 重切会生成全新 chunk_id：分段缓存和引用本文档的 QA 语义缓存全部失效，
        # 否则用户仍会命中带旧引用的陈旧答案
        await _invalidate_caches_after_resplit(doc_id)

        # 3. 切片
        # title 预置文档标题：无显式章节标题的文本（如 PDF 正文）也能带可辨认的引用标题
        base_meta = {MetaKey.DOC_ID: doc_id, MetaKey.DOC_VERSION: version_id,
                     MetaKey.ACCESSIBLE_BY: ctx["accessible_by"] or "VISITOR",
                     MetaKey.TITLE: ctx["doc_title"]}
        if file_type in ("excel", "csv"):
            rows = await _run_blocking(ExcelSplitter.load_rows, src_path, file_type)
            chunks = ExcelSplitter(mode=TableMode.KV,
                                   chunk_size=conf.get("chunk_size", 800)).split_sheet(rows, base_meta)
            chunks += _image_note_chunks(ctx, base_meta, start_order=len(chunks))
        else:
            splitter = create_splitter(
                conf.get("split_type", SplitType.SMART),
                chunk_size=conf.get("chunk_size", 800),
                overlap=conf.get("overlap", 0),
                title_level=conf.get("title_level", 0),
                regex=conf.get("regex"), separator=conf.get("separator"))
            chunks = await _run_blocking(splitter.split, content_text, base_meta)

        # 4. 分段落库（STORED，向量待阶段三补齐）
        seg_rows = []
        for c in chunks:
            meta = {**c.metadata,
                    MetaKey.PARENT_CHUNK_ID: c.metadata.get(MetaKey.PARENT_CHUNK_ID),
                    MetaKey.BROTHER_CHUNK_ID: c.metadata.get(MetaKey.BROTHER_CHUNK_ID)}
            seg_rows.append(KnowledgeSegment(
                chunk_id=meta[MetaKey.CHUNK_ID], text=c.text, metadata_=meta,
                document_id=doc_id, document_version=version_id,
                tenant_id=ctx["tenant_id"],
                chunk_order=meta.get(MetaKey.ORDER, 0),
                status=SegmentStatus.STORED,
                skip_embedding=meta.get(MetaKey.SKIP_EMBEDDING, 0),
            ))
        async with session_scope() as session:
            session.add_all(seg_rows)
        await _set_status(doc_id, version_id, DocumentStatus.CHUNKED, DocumentStatus.CHUNKED)
        await _record_stage(doc_id, version_id, "split", _STAGE_SUCCEEDED,
                            f"生成 {len(chunks)} 个分段")
        logger.info("stage_chunked", doc_id=doc_id, segments=len(chunks))
        get_event_bus().publish(DocumentChunked(doc_id=doc_id, version_id=version_id))
    except Exception as e:
        logger.error("stage_split_failed", doc_id=doc_id, error=str(e),
                     tb=traceback.format_exc()[-1500:])
        await _set_status(doc_id, version_id, DocumentStatus.FAILED, DocumentStatus.FAILED)
        await _record_stage(doc_id, version_id, "split", _STAGE_FAILED, str(e))


# ------------------------------------------------------------------
# 阶段三：向量化（分布式锁互斥）
# ------------------------------------------------------------------
async def _on_chunked(event: DocumentChunked) -> None:
    lock = get_lock()
    try:
        async with lock.hold("document-embedding", str(event.doc_id), wait_time=0):
            await _embedding_pipeline(event.doc_id, event.version_id)
    except Exception as e:
        if "重复提交" in str(e):
            logger.info("embedding_skip_locked", doc_id=event.doc_id)
            return
        logger.error("stage_embedding_blocked", doc_id=event.doc_id, error=str(e))


async def _embedding_pipeline(doc_id: int, version_id: int) -> None:
    from app.services.embedding import get_embedding

    ctx = await _load_pipeline_ctx(doc_id, version_id)
    if ctx is None:
        return
    await _record_stage(doc_id, version_id, "embedding", _STAGE_STARTED)
    expire_ts = int(ctx["expire_date"].timestamp()) if ctx["expire_date"] else None
    try:
        # 只处理本版本 STORED / FAILED 且未跳过的分段（幂等：VECTOR_STORED 不再处理）
        async with session_scope() as session:
            segments = (await session.execute(
                select(KnowledgeSegment).where(
                    KnowledgeSegment.document_id == doc_id,
                    KnowledgeSegment.document_version == version_id,
                    KnowledgeSegment.deleted == 0)
                .order_by(KnowledgeSegment.chunk_order))).scalars().all()

            embed_targets = [s for s in segments if not s.skip_embedding
                             and s.status != SegmentStatus.VECTOR_STORED]
            # 分批向量化（批大小与 embedding 服务一致）
            vectors = []
            for i in range(0, len(embed_targets), 10):
                batch = embed_targets[i:i + 10]
                vectors.extend(await get_embedding().embed([s.text for s in batch]))
            for s, v in zip(embed_targets, vectors):
                s.embedding = v.tobytes()
                s.status = SegmentStatus.VECTOR_STORED

        # 写向量存储（父分段 skipEmbedding=1 不参与检索，但保留在库中供回溯；
        # ES 中不写入 vector 字段）
        store = await get_vector_store()
        records = [ChunkRecord(
            chunk_id=s.chunk_id, text=s.text, document_id=doc_id, doc_version=version_id,
            title=(s.metadata_ or {}).get(MetaKey.TITLE, ""), order=s.chunk_order,
            skip_embedding=s.skip_embedding,
            accessible_by=(s.metadata_ or {}).get(MetaKey.ACCESSIBLE_BY, "VISITOR"),
            expire_at=expire_ts, tenant_id=ctx["tenant_id"],
            vector=list(map(float, np.frombuffer(s.embedding, dtype=np.float32)))
            if (s.embedding and not s.skip_embedding) else None,
        ) for s in segments]
        written = await store.upsert(records)

        await _set_status(doc_id, version_id, DocumentStatus.VECTOR_STORED,
                          DocumentStatus.VECTOR_STORED)
        await _record_stage(doc_id, version_id, "embedding", _STAGE_SUCCEEDED,
                            f"写入 {written} 条向量")
        logger.info("stage_vector_stored", doc_id=doc_id, stored=written,
                    backend=store.backend_name())
    except Exception as e:
        logger.error("stage_embedding_failed", doc_id=doc_id, error=str(e),
                     tb=traceback.format_exc()[-1500:])
        await _set_status(doc_id, version_id, DocumentStatus.FAILED, DocumentStatus.FAILED)
        await _record_stage(doc_id, version_id, "embedding", _STAGE_FAILED, str(e))


# ------------------------------------------------------------------
# 辅助
# ------------------------------------------------------------------
async def _purge_segments(doc_id: int, version_id: int) -> None:
    """重切片前清旧数据：分段软删 + 向量存储清理（两实现语义已统一为软删/删索引）。"""
    store = await get_vector_store()
    await store.delete_by_version(version_id)
    async with session_scope() as session:
        await session.execute(
            update(KnowledgeSegment)
            .where(KnowledgeSegment.document_id == doc_id,
                   KnowledgeSegment.document_version == version_id,
                   KnowledgeSegment.deleted == 0)
            .values(deleted=1))


async def _invalidate_caches_after_resplit(doc_id: int) -> None:
    """重新切片后失效缓存：分段 L1/L2 缓存 + 引用该文档的 QA 语义缓存。

    chunk_id 每次重切都会变，旧缓存键再也命中不到；QA 缓存里保存的是来源快照，
    不清理会继续返回引用旧 chunk_id 的答案。
    """
    from app.modules.document.service import invalidate_doc_cache
    from app.modules.qa_cache import service as qa_service
    await invalidate_doc_cache(doc_id)
    await qa_service.invalidate_by_document(doc_id, detail=f"文档 {doc_id} 重新切片")


def _run_blocking(fn, *args):
    """同步 CPU 函数放线程池（openpyxl 等大解析不阻塞事件循环）。"""
    import anyio
    return anyio.to_thread.run_sync(fn, *args)


def register_listeners() -> None:
    """应用启动时注册文档流水线监听器（幂等：重复调用会叠加，仅调用一次）。"""
    bus = get_event_bus()
    bus.subscribe(DocumentUploaded, _on_uploaded)
    bus.subscribe(DocumentConverted, _on_converted)
    bus.subscribe(DocumentChunked, _on_chunked)
