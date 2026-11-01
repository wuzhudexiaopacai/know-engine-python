"""文档查询/管理 Service + 分段三级缓存读取 + 版本文件预览。

缓存层级：
    L1 进程内 TTL-LRU → L2 Redis(30s) → L3 数据库；负缓存防击穿。
父子分段回溯（parentChunkId）在检索热路径上，每一跳都走该缓存。

删除语义（本次重构重点）：
    文档删除 = 主表软删 + 版本软删 + 分段软删 + 向量清理 + 对象存储清理
               + 秒传标记清理 + 分段缓存/派生 QA 缓存失效
    版本删除 = 版本软删 + 该版本分段软删 + 该版本向量/文件清理
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import zipfile
from pathlib import Path

import numpy as np
from sqlalchemy import and_, case, func, select, update
from starlette.concurrency import run_in_threadpool

from app.core.database import session_scope
from app.core.exceptions import NotFoundError, ParamError
from app.core.logging import get_logger
from app.infra.cache import get_cache
from app.infra.file_storage import get_file_store
from app.models.constants import DocumentStatus, MetaKey, SegmentStatus
from app.models.entities import DocumentVersion, KnowledgeDocument, KnowledgeSegment
from app.models.queries import base_filter, soft_delete, update_with_lock
from app.modules.document.schemas import (
    DocumentVO, DocumentVersionVO, SegmentListVO, SegmentVO,
)
from app.modules.document.upload_service import done_marker_key

logger = get_logger("app.document")

# 预览 MIME：按文件后缀判定，未知一律二进制流
_MEDIA_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".doc": "application/msword",
}


# ------------------------------------------------------------------
# 分段读取（三级缓存）
# ------------------------------------------------------------------
async def get_segment_text_by_chunk_id(chunk_id: str) -> str | None:
    """按 chunkId 取分段全文（父分段回溯入口）。

    cache key 前缀 ke:chunk:；L2 TTL 30s；查无数据写空串负缓存（cache.get 内置）。
    """
    key = f"ke:chunk:{chunk_id}"

    async def _load() -> str | None:
        async with session_scope() as session:
            row = (await session.execute(
                select(KnowledgeSegment.text).where(
                    KnowledgeSegment.chunk_id == chunk_id,
                    KnowledgeSegment.deleted == 0))).scalar_one_or_none()
            return row

    return await get_cache().get(key, loader=_load, ttl=30)


# ------------------------------------------------------------------
# 文档管理
# ------------------------------------------------------------------
async def list_documents(page: int, size: int, status: str | None,
                         keyword: str | None) -> dict:
    """分页列表（软删过滤 + 关键字模糊 + 状态筛选）。"""
    async with session_scope() as session:
        stmt = base_filter(KnowledgeDocument)
        count_stmt = select(func.count()).select_from(KnowledgeDocument)
        cond = []
        if status:
            cond.append(KnowledgeDocument.status == status)
        if keyword:
            cond.append(KnowledgeDocument.doc_title.like(f"%{keyword}%"))
        for c in cond:
            stmt = stmt.where(c)
            count_stmt = count_stmt.where(c)

        total = (await session.execute(count_stmt)).scalar_one()
        rows = (await session.execute(
            stmt.order_by(KnowledgeDocument.doc_id.desc())
            .offset((page - 1) * size).limit(size))).scalars().all()

        # 版本号与分段数批量补齐（两次聚合查询，避免 N+1）
        doc_ids = [r.doc_id for r in rows]
        seg_counts = {}
        version_map = {}
        if doc_ids:
            current_ids = [r.current_version_id for r in rows if r.current_version_id]
            # 只统计当前激活版本的分段（含历史版本会让列表数字虚高）
            seg_rows = (await session.execute(
                select(KnowledgeSegment.document_id, func.count())
                .where(KnowledgeSegment.document_id.in_(doc_ids),
                       KnowledgeSegment.document_version.in_(current_ids),
                       KnowledgeSegment.deleted == 0,
                       KnowledgeSegment.skip_embedding == 0)
                .group_by(KnowledgeSegment.document_id))).all() if current_ids else []
            seg_counts = {d: c for d, c in seg_rows}
            ver_rows = (await session.execute(
                select(DocumentVersion.version_id, DocumentVersion.version)
                .where(DocumentVersion.version_id.in_(
                    [r.current_version_id for r in rows if r.current_version_id])))).all()
            version_map = dict(ver_rows)

        records = []
        for r in rows:
            vo = DocumentVO.model_validate(r)
            vo.segment_count = seg_counts.get(r.doc_id, 0)
            vo.current_version = version_map.get(r.current_version_id or 0)
            records.append(vo)
        return {"total": total, "page_num": page, "page_size": size, "records": records}


async def get_document_detail(doc_id: int) -> DocumentVO:
    async with session_scope() as session:
        doc = (await session.execute(
            base_filter(KnowledgeDocument).where(KnowledgeDocument.doc_id == doc_id))
        ).scalar_one_or_none()
        if doc is None:
            raise NotFoundError("文档不存在")
        vo = DocumentVO.model_validate(doc)
        if doc.current_version_id:
            vo.segment_count = (await session.execute(
                select(func.count()).select_from(KnowledgeSegment).where(
                    KnowledgeSegment.document_id == doc_id,
                    KnowledgeSegment.document_version == doc.current_version_id,
                    KnowledgeSegment.deleted == 0,
                    KnowledgeSegment.skip_embedding == 0))).scalar_one()
            vo.current_version = (await session.execute(
                select(DocumentVersion.version)
                .where(DocumentVersion.version_id == doc.current_version_id))).scalar_one_or_none()
        return vo


async def list_versions(doc_id: int) -> list[DocumentVersionVO]:
    async with session_scope() as session:
        doc = (await session.execute(
            base_filter(KnowledgeDocument).where(KnowledgeDocument.doc_id == doc_id))
        ).scalar_one_or_none()
        if doc is None:
            raise NotFoundError("文档不存在")
        rows = (await session.execute(
            base_filter(DocumentVersion).where(DocumentVersion.doc_id == doc_id)
            .order_by(DocumentVersion.version_id.desc()))).scalars().all()
        return [DocumentVersionVO.model_validate(v) for v in rows]


async def switch_version(doc_id: int, version_id: int, lock_version: int) -> None:
    """版本热切换：仅改 current_version_id（检索按"当前可用版本"过滤，无需重新向量化）。

    只允许切换到已完成向量化的版本，且版本必须属于该文档。
    """
    async with session_scope() as session:
        ver = (await session.execute(
            base_filter(DocumentVersion).where(
                DocumentVersion.version_id == version_id,
                DocumentVersion.doc_id == doc_id))).scalar_one_or_none()
        if ver is None:
            raise NotFoundError("版本不存在或不属于该文档")
        if ver.status != DocumentStatus.VECTOR_STORED:
            raise ParamError("仅可切换到已完成向量化的版本")
        await update_with_lock(session, KnowledgeDocument, "doc_id", doc_id,
                               {"current_version_id": version_id}, lock_version)
        await session.execute(
            update(KnowledgeDocument)
            .where(KnowledgeDocument.doc_id == doc_id)
            .values(status=ver.status))
    await invalidate_doc_cache(doc_id)


async def rollback_delete_version(doc_id: int, version_id: int) -> None:
    """删除指定版本（逻辑删除）：

    - 版本行软删 + 该版本分段软删
    - 该版本原始/转换文件、向量索引清理
    - 若删除的是当前版本，回退至最新存活版本
    """
    keys: list[str] = []
    async with session_scope() as session:
        doc = (await session.execute(
            base_filter(KnowledgeDocument).where(KnowledgeDocument.doc_id == doc_id))
        ).scalar_one_or_none()
        if doc is None:
            raise NotFoundError("文档不存在")
        ver = (await session.execute(
            base_filter(DocumentVersion).where(
                DocumentVersion.version_id == version_id,
                DocumentVersion.doc_id == doc_id))).scalar_one_or_none()
        if ver is None:
            raise NotFoundError("版本不存在或不属于该文档")
        keys = _version_file_keys(ver)

        await soft_delete(session, DocumentVersion, "version_id", version_id)
        await session.execute(
            update(KnowledgeSegment)
            .where(KnowledgeSegment.document_id == doc_id,
                   KnowledgeSegment.document_version == version_id,
                   KnowledgeSegment.deleted == 0)
            .values(deleted=1))

        if doc.current_version_id == version_id:
            latest = (await session.execute(
                base_filter(DocumentVersion).where(DocumentVersion.doc_id == doc_id)
                .order_by(DocumentVersion.version_id.desc()).limit(1))).scalars().first()
            doc.current_version_id = latest.version_id if latest else None
            doc.status = latest.status if latest else DocumentStatus.FAILED
            session.add(doc)

    await _remove_files(keys)
    from app.infra.vector_store import get_vector_store
    store = await get_vector_store()
    await store.delete_by_version(version_id)
    await invalidate_doc_cache(doc_id)


async def delete_document(doc_id: int, lock_version: int) -> None:
    """文档级联删除：主表软删 + 版本/分段软删 + 文件/向量/缓存/秒传标记清理。"""
    keys: list[str] = []
    extension: dict = {}
    async with session_scope() as session:
        doc = (await session.execute(
            base_filter(KnowledgeDocument).where(KnowledgeDocument.doc_id == doc_id))
        ).scalar_one_or_none()
        if doc is None:
            raise NotFoundError("文档不存在")
        extension = dict(doc.extension or {})
        versions = (await session.execute(
            base_filter(DocumentVersion).where(DocumentVersion.doc_id == doc_id))).scalars().all()
        keys = [k for v in versions for k in _version_file_keys(v)]

        await soft_delete(session, KnowledgeDocument, "doc_id", doc_id, lock_version)
        await session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.doc_id == doc_id, DocumentVersion.deleted == 0)
            .values(deleted=1))
        await session.execute(
            update(KnowledgeSegment)
            .where(KnowledgeSegment.document_id == doc_id, KnowledgeSegment.deleted == 0)
            .values(deleted=1))

    await _remove_files(keys)
    from app.infra.vector_store import get_vector_store
    store = await get_vector_store()
    await store.delete_by_document(doc_id)
    await invalidate_doc_cache(doc_id)
    await _delete_upload_markers(extension)
    # 删除文档 → 失效引用它的 QA 语义缓存条目
    from app.modules.qa_cache import service as qa_service
    await qa_service.invalidate_by_document(doc_id, detail=f"文档 {doc_id} 已删除")


async def retry_pipeline(doc_id: int, version_id: int, stage: str,
                         force: bool = False) -> None:
    """手动重试：按阶段重发事件（与补偿任务同一入口，复用幂等防护）。

    force=True 供「重新切片」使用：允许对已 VECTOR_STORED 版本重跑。
    """
    async with session_scope() as session:
        ver = (await session.execute(
            base_filter(DocumentVersion).where(
                DocumentVersion.version_id == version_id,
                DocumentVersion.doc_id == doc_id))).scalar_one_or_none()
        if ver is None:
            raise NotFoundError("版本不存在或不属于该文档")

    from app.infra.event_bus import get_event_bus
    from app.modules.document.events import DocumentChunked, DocumentConverted, DocumentUploaded

    bus = get_event_bus()
    if stage == "convert":
        bus.publish(DocumentUploaded(doc_id=doc_id, version_id=version_id, force=force))
    elif stage == "split":
        bus.publish(DocumentConverted(doc_id=doc_id, version_id=version_id, force=force))
    elif stage == "embedding":
        bus.publish(DocumentChunked(doc_id=doc_id, version_id=version_id))
    else:
        raise ParamError(f"未知阶段: {stage}")


# ------------------------------------------------------------------
# 版本预览
# ------------------------------------------------------------------
async def get_preview_file(doc_id: int, version_id: int,
                           kind: str) -> tuple[Path, str, str]:
    """返回 (本地可读路径, MIME, 展示文件名)，供 Controller 构造 inline 响应。"""
    async with session_scope() as session:
        doc = (await session.execute(
            base_filter(KnowledgeDocument).where(KnowledgeDocument.doc_id == doc_id))
        ).scalar_one_or_none()
        if doc is None:
            raise NotFoundError("文档不存在")
        ver = (await session.execute(
            base_filter(DocumentVersion).where(
                DocumentVersion.version_id == version_id,
                DocumentVersion.doc_id == doc_id))).scalar_one_or_none()
        if ver is None:
            raise NotFoundError("版本不存在或不属于该文档")
        key = ver.converted_doc_url if kind == "converted" else ver.doc_url
        if kind == "converted" and not key:
            key = ver.doc_url
        title = doc.doc_title

    if not key:
        raise NotFoundError("该版本暂无可预览文件")

    path = await get_file_store().get(key)
    suffix = Path(key).suffix.lower()
    media = _MEDIA_TYPES.get(suffix, "application/octet-stream")
    if kind == "converted" and suffix in (".md", ".markdown"):
        filename = f"{Path(title).stem}.md"
    else:
        filename = title
    return path, media, filename


# 预览渲染上限：Office 预览只做"看一眼"，超大表格/文档截断并提示下载
_MAX_ROWS = 200
_MAX_COLS = 30
_MAX_SHEETS = 10


async def get_preview_content(doc_id: int, version_id: int, kind: str) -> dict:
    """版本预览的渲染数据（Word/Excel 不再靠浏览器内嵌，避免直接触发下载）。

    - markdown：md / txt 原文，或 docx 经内置解析器转 Markdown
    - sheet：xlsx / csv 读取二维表（行列截断）
    - file：PDF / 图片等浏览器可内嵌类型，前端走 iframe
    - unsupported：旧版 .doc/.xls 等无法解析，只能下载
    """
    path, _media, filename = await get_preview_file(doc_id, version_id, kind)
    suffix = path.suffix.lower()

    if suffix in (".md", ".markdown", ".txt"):
        text = await run_in_threadpool(_read_text, path)
        return {"mode": "markdown", "text": text, "filename": filename}
    if suffix == ".docx":
        from app.modules.document.converter import DocumentConverter
        text = await run_in_threadpool(DocumentConverter._docx_to_markdown, path)
        return {"mode": "markdown", "text": text, "filename": filename}
    if suffix == ".xlsx":
        sheets = await run_in_threadpool(_sheet_rows, path)
        return {"mode": "sheet", "sheets": sheets, "filename": filename}
    if suffix == ".csv":
        text = await run_in_threadpool(_read_text, path)
        return {"mode": "sheet", "sheets": [_csv_rows(text)], "filename": filename}
    if suffix in (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        return {"mode": "file", "filename": filename}
    return {"mode": "unsupported", "filename": filename}


# Markdown 中指向对象存储的本地图片链接（转换阶段统一写成 /api/files/{key}）
_LOCAL_IMG_RE = re.compile(r"/api/files/([^)\s\"'<>]+)")


async def get_markdown_bundle(doc_id: int, version_id: int,
                              kind: str = "converted") -> tuple[Path, str]:
    """把 Markdown 与其引用的本地图片打包成 ZIP（图片链接改写为 images/ 相对路径）。

    下载后的 Markdown 离线也能看图，而不是留下指向服务器的 /api/files 链接。
    取图走对象存储回读缓存（MinIO 首次下载后会留在本地），并发 8，避免几十张图串行拖慢。
    """
    path, _media, filename = await get_preview_file(doc_id, version_id, kind)
    if path.suffix.lower() not in (".md", ".markdown", ".txt"):
        raise ParamError("当前文件不是 Markdown，请在预览中直接下载原文件")

    text = await run_in_threadpool(_read_text, path)
    keys = list(dict.fromkeys(_LOCAL_IMG_RE.findall(text)))

    store = get_file_store()
    sem = asyncio.Semaphore(8)

    async def fetch(key: str) -> tuple[str, bytes | None]:
        async with sem:
            try:
                img_path = await store.get(key)
                return key, await run_in_threadpool(img_path.read_bytes)
            except Exception as e:   # 图片丢失不影响 Markdown 本身
                logger.warning("bundle_image_missing", key=key, error=str(e))
                return key, None

    fetched = await asyncio.gather(*(fetch(k) for k in keys)) if keys else []
    entries: list[tuple[str, bytes]] = []
    for key, data in fetched:
        if data is None:
            continue
        name = key.rsplit("/", 1)[-1]
        text = text.replace(f"/api/files/{key}", f"images/{name}")
        entries.append((f"images/{name}", data))

    stem = re.sub(r"[\\/]", "_", filename.rsplit(".", 1)[0] or "document")
    fd, tmp_name = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    tmp = Path(tmp_name)

    def _write() -> None:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{stem}.md", text.encode("utf-8"))
            for name, data in entries:
                zf.writestr(name, data)

    await run_in_threadpool(_write)
    return tmp, f"{stem}.zip"


def _read_text(path: Path) -> str:
    """文本直读（多编码尝试）。"""
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _sheet_rows(path: Path) -> list[dict]:
    """xlsx → 各工作表二维字符串数组（只读模式，限制行列防止大表拖垮预览）。"""
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    try:
        sheets = []
        for ws in wb.worksheets[:_MAX_SHEETS]:
            rows, truncated = [], len(wb.worksheets) > _MAX_SHEETS
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= _MAX_ROWS:
                    truncated = True
                    break
                if len(row) > _MAX_COLS:
                    truncated = True
                rows.append(["" if v is None else str(v) for v in row[:_MAX_COLS]])
            sheets.append({"name": ws.title, "rows": rows, "truncated": truncated})
        return sheets
    finally:
        wb.close()


def _csv_rows(text: str) -> dict:
    """csv 文本 → 二维字符串数组（同样行列截断）。"""
    import csv
    import io

    rows, truncated = [], False
    for i, row in enumerate(csv.reader(io.StringIO(text))):
        if i >= _MAX_ROWS:
            truncated = True
            break
        if len(row) > _MAX_COLS:
            truncated = True
        rows.append(row[:_MAX_COLS])
    return {"name": "CSV", "rows": rows, "truncated": truncated}


# ------------------------------------------------------------------
# 切片 / 向量化结果（流程可视化）
# ------------------------------------------------------------------
async def list_segments(doc_id: int, version_id: int, page: int, size: int) -> SegmentListVO:
    """分页返回指定版本的分段与向量化摘要，供前端查看"分片效果/向量效果"。"""
    async with session_scope() as session:
        ver = (await session.execute(
            base_filter(DocumentVersion).where(
                DocumentVersion.version_id == version_id,
                DocumentVersion.doc_id == doc_id))).scalar_one_or_none()
        if ver is None:
            raise NotFoundError("版本不存在或不属于该文档")

        cond = (KnowledgeSegment.document_id == doc_id,
                KnowledgeSegment.document_version == version_id,
                KnowledgeSegment.deleted == 0)
        total = (await session.execute(
            select(func.count()).select_from(KnowledgeSegment).where(*cond))).scalar_one()
        embedded_count, skipped_count = (await session.execute(
            select(
                func.sum(case((and_(
                    KnowledgeSegment.skip_embedding == 0,
                    KnowledgeSegment.status == SegmentStatus.VECTOR_STORED), 1), else_=0)),
                func.sum(case((KnowledgeSegment.skip_embedding == 1, 1), else_=0)))
            .where(*cond))).one()
        rows = (await session.execute(
            select(KnowledgeSegment).where(*cond)
            .order_by(KnowledgeSegment.chunk_order, KnowledgeSegment.id)
            .offset((page - 1) * size).limit(size))).scalars().all()

        # 向量维度取第一个已向量化分段（全库同一 embedding 模型，维度一致）
        dim_row = (await session.execute(
            select(KnowledgeSegment.embedding).where(
                KnowledgeSegment.document_version == version_id,
                KnowledgeSegment.deleted == 0,
                KnowledgeSegment.embedding.is_not(None)).limit(1))).scalar_one_or_none()

    vector_dim = len(dim_row) // 4 if dim_row else 0   # float32 = 4 bytes
    records: list[SegmentVO] = []
    for s in rows:
        meta = s.metadata_ or {}
        dim, norm, preview = 0, None, []
        if s.embedding:
            vec = np.frombuffer(s.embedding, dtype=np.float32)
            dim = int(vec.size)
            if dim:
                norm = round(float(np.linalg.norm(vec)), 4)
                preview = [round(float(x), 4) for x in vec[:12]]
        text = s.text if len(s.text) <= 1200 else s.text[:1200] + "…"
        records.append(SegmentVO(
            chunk_id=s.chunk_id, text=text, chunk_order=s.chunk_order, status=s.status,
            skip_embedding=s.skip_embedding,
            parent_chunk_id=meta.get(MetaKey.PARENT_CHUNK_ID),
            brother_chunk_id=meta.get(MetaKey.BROTHER_CHUNK_ID),
            embedding_dim=dim, vector_norm=norm, vector_preview=preview,
            metadata_=meta))
    return SegmentListVO(
        total=total, page=page, page_size=size,
        embedded_count=int(embedded_count or 0), skipped_count=int(skipped_count or 0),
        vector_dim=vector_dim, records=records)


# ------------------------------------------------------------------
# 当前可用版本（检索过滤）
# ------------------------------------------------------------------
async def active_version_ids() -> list[int]:
    """返回所有非删除文档当前应参与检索的版本 ID。

    规则：优先当前激活版本；当前版本尚未向量化（正在处理/失败）时，
    回退到该文档最新一个 VECTOR_STORED 版本，保证检索不中断。
    """
    async with session_scope() as session:
        docs = (await session.execute(
            base_filter(KnowledgeDocument)
            .where(KnowledgeDocument.current_version_id.is_not(None)))).scalars().all()
        if not docs:
            return []
        rows = (await session.execute(
            select(DocumentVersion.doc_id, DocumentVersion.version_id,
                   DocumentVersion.status)
            .where(DocumentVersion.deleted == 0,
                   DocumentVersion.doc_id.in_([d.doc_id for d in docs])))).all()

    grouped: dict[int, list] = {}
    for row in rows:
        grouped.setdefault(row.doc_id, []).append(row)

    result: list[int] = []
    for doc in docs:
        versions = grouped.get(doc.doc_id, [])
        current = next((v for v in versions if v.version_id == doc.current_version_id), None)
        if current is not None and current.status == DocumentStatus.VECTOR_STORED:
            result.append(current.version_id)
            continue
        ready = [v for v in versions if v.status == DocumentStatus.VECTOR_STORED]
        if ready:
            result.append(max(ready, key=lambda v: v.version_id).version_id)
    return result


# ------------------------------------------------------------------
# 内部工具
# ------------------------------------------------------------------
def _version_file_keys(ver: DocumentVersion) -> list[str]:
    keys = [ver.doc_url, ver.converted_doc_url]
    return [k for k in keys if k]


async def _remove_files(keys: list[str]) -> None:
    """删除对象存储文件（幂等，单文件失败不影响其它清理）。"""
    store = get_file_store()
    for key in dict.fromkeys(keys):
        try:
            await store.delete(key)
        except Exception as e:  # pragma: no cover - 存储侧异常仅记录
            logger.warning("file_delete_failed", key=key, error=str(e))


async def _delete_upload_markers(extension: dict) -> None:
    """清理该文档所有版本的秒传标记，避免删除后重传"命中不操作"。"""
    md5s: list[str] = [str(x).lower() for x in (extension.get("fileMd5s") or []) if x]
    for entry in (extension.get("versions") or {}).values():
        if isinstance(entry, dict) and entry.get("md5"):
            md5s.append(str(entry["md5"]).lower())
    if extension.get("fileMd5"):
        md5s.append(str(extension["fileMd5"]).lower())

    cache = get_cache()
    for md5 in dict.fromkeys(md5s):
        await cache.delete(done_marker_key(md5))


async def invalidate_doc_cache(doc_id: int) -> None:
    """文档变更 → 清理该文档全部分段的 L1/L2 缓存（权限或版本变更时失效）。

    重新切片同样要调用：chunk_id 是随机雪花号，重切后旧缓存键永久失配。
    """
    async with session_scope() as session:
        chunk_ids = (await session.execute(
            select(KnowledgeSegment.chunk_id).where(
                KnowledgeSegment.document_id == doc_id))).scalars().all()
    cache = get_cache()
    for cid in chunk_ids:
        await cache.delete(f"ke:chunk:{cid}")
