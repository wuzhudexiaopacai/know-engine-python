"""文档接入与管理 REST 接口（分片上传 + 查询/版本/预览/重试，均限管理员）。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.core.deps import CurrentUser, require_admin
from app.core.exceptions import ParamError
from app.core.response import R
from app.modules.document import service
from app.modules.document.schemas import (
    ChunkFinalizeRequest, ChunkInitRequest, SplitConfigUpdate,
)
from app.modules.document.upload_service import get_upload_service

router = APIRouter(prefix="/api/document", tags=["知识库文档"])


# ------------------------------------------------------------------
# 分片上传
# ------------------------------------------------------------------
@router.post("/upload/init")
async def upload_init(req: ChunkInitRequest,
                      user: CurrentUser = Depends(require_admin)) -> R:
    return R.ok(await get_upload_service().init(req, user.sub))


@router.post("/upload/chunk")
async def upload_chunk(upload_id: str = Form(...), index: int = Form(...),
                       chunk_md5: str | None = Form(default=None),
                       file: UploadFile = File(...),
                       user: CurrentUser = Depends(require_admin)) -> R:
    data = await file.read()
    return R.ok(await get_upload_service().put_chunk(upload_id, index, data, user.sub, chunk_md5))


@router.get("/upload/status")
async def upload_status(upload_id: str = Query(...),
                        user: CurrentUser = Depends(require_admin)) -> R:
    return R.ok(await get_upload_service().status(upload_id, user.sub))


@router.post("/upload/finalize")
async def upload_finalize(req: ChunkFinalizeRequest,
                          user: CurrentUser = Depends(require_admin)) -> R:
    return R.ok(await get_upload_service().finalize(req, user.sub))


# ------------------------------------------------------------------
# 文档查询与管理
# ------------------------------------------------------------------
@router.get("/list")
async def list_documents(page: int = Query(1, ge=1), size: int = Query(10, ge=1, le=100),
                         status: str | None = None, keyword: str | None = None,
                         user: CurrentUser = Depends(require_admin)) -> R:
    return R.ok(await service.list_documents(page, size, status, keyword))


@router.get("/{doc_id}")
async def document_detail(doc_id: int, user: CurrentUser = Depends(require_admin)) -> R:
    return R.ok(await service.get_document_detail(doc_id))


@router.get("/{doc_id}/versions")
async def document_versions(doc_id: int, user: CurrentUser = Depends(require_admin)) -> R:
    return R.ok(await service.list_versions(doc_id))


@router.get("/{doc_id}/versions/{version_id}/preview")
async def preview_version(doc_id: int, version_id: int,
                          kind: str = Query("converted", pattern="^(converted|original)$"),
                          download: bool = Query(False),
                          user: CurrentUser = Depends(require_admin)):
    """版本文件预览：converted=转换后 Markdown/直通文件，original=原始文件。

    默认以 inline 返回（浏览器内嵌 PDF/图片）；download=1 改为 attachment 触发下载。
    带 ?token= 便于新窗口直开。
    """
    path, media_type, filename = await service.get_preview_file(doc_id, version_id, kind)
    disposition = ("attachment" if download else "inline") + f"; filename*=UTF-8''{quote(filename)}"
    return FileResponse(path, media_type=media_type,
                        headers={"Content-Disposition": disposition})


@router.get("/{doc_id}/versions/{version_id}/preview/bundle")
async def preview_bundle(doc_id: int, version_id: int,
                         kind: str = Query("converted", pattern="^(converted|original)$"),
                         user: CurrentUser = Depends(require_admin)):
    """Markdown 打包下载：Markdown + images/（链接改写为相对路径），下载后离线可看图。"""
    path, filename = await service.get_markdown_bundle(doc_id, version_id, kind)
    return FileResponse(
        path, media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        background=BackgroundTask(path.unlink, missing_ok=True))


@router.get("/{doc_id}/versions/{version_id}/preview/content")
async def preview_content(doc_id: int, version_id: int,
                          kind: str = Query("converted", pattern="^(converted|original)$"),
                          user: CurrentUser = Depends(require_admin)) -> R:
    """Word/Excel 预览渲染数据：docx 转 Markdown、xlsx/csv 转二维表，避免浏览器下载。"""
    return R.ok(await service.get_preview_content(doc_id, version_id, kind))


@router.get("/{doc_id}/versions/{version_id}/segments")
async def version_segments(doc_id: int, version_id: int,
                           page: int = Query(1, ge=1), size: int = Query(10, ge=1, le=50),
                           user: CurrentUser = Depends(require_admin)) -> R:
    """切片与向量化结果：分段正文、父子关系、向量维度/模长/前若干维数值。"""
    return R.ok(await service.list_segments(doc_id, version_id, page, size))


@router.post("/{doc_id}/switch-version")
async def switch_version(doc_id: int, version_id: int, lock_version: int,
                         user: CurrentUser = Depends(require_admin)) -> R:
    await service.switch_version(doc_id, version_id, lock_version)
    return R.ok(None, "已切换激活版本")


@router.post("/{doc_id}/delete-version")
async def delete_version(doc_id: int, version_id: int,
                         user: CurrentUser = Depends(require_admin)) -> R:
    await service.rollback_delete_version(doc_id, version_id)
    return R.ok(None, "版本已删除")


@router.delete("/{doc_id}")
async def delete_document(doc_id: int, lock_version: int = Query(0),
                          user: CurrentUser = Depends(require_admin)) -> R:
    await service.delete_document(doc_id, lock_version)
    return R.ok(None, "文档已删除")


@router.post("/{doc_id}/retry")
async def retry(doc_id: int, version_id: int, stage: str,
                user: CurrentUser = Depends(require_admin)) -> R:
    """手动重试某阶段（convert/split/embedding），复用补偿任务同一幂等入口。"""
    await service.retry_pipeline(doc_id, version_id, stage)
    return R.ok(None, "已重新触发处理")


@router.post("/{doc_id}/resplit")
async def resplit(doc_id: int, body: SplitConfigUpdate, version_id: int,
                  user: CurrentUser = Depends(require_admin)) -> R:
    """修改切片配置并对指定版本重跑 切片→向量化（force 允许已就绪版本重切）。"""
    await _update_split_config(doc_id, body)
    await service.retry_pipeline(doc_id, version_id, "split", force=True)
    return R.ok(None, "已按新配置重新切片")


async def _update_split_config(doc_id: int, body: SplitConfigUpdate) -> None:
    from sqlalchemy import select

    from app.core.database import session_scope
    from app.models.entities import KnowledgeDocument

    async with session_scope() as session:
        doc = (await session.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.doc_id == doc_id,
                                            KnowledgeDocument.deleted == 0))
        ).scalar_one_or_none()
        if doc is None:
            raise ParamError("文档不存在")
        doc.extension = {**(doc.extension or {}), "splitConfig": body.model_dump()}
        session.add(doc)
