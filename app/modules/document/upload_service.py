"""大文件分片上传服务（企业级断点续传 + 秒传 + 完整性校验）。

流程（前端契约）：
    1. POST /init      校验大小/秒传命中/返回 upload_id 与已传分片列表（断点续传）
    2. POST /chunk     逐片上传（乱序可、重复可，幂等覆盖写）
    3. GET  /status    查询进度（弱网恢复后对账）
    4. POST /finalize  合并 → MD5 校验 → 魔数类型检测 → 入 MinIO → 建文档/版本 → 发事件

秒传语义（本次重构重点）：
- 秒传标记值存 JSON（doc_id/version_id/doc_url），命中后必须校验目标文档与版本仍然存活，
  否则立即清理标记并走正常上传；删除文档时也会同步清理标记，避免"删了再传显示命中"。
- 只有「新建文档」才允许秒传；带 doc_id 的「上传新版本」绝不走秒传（同内容也要产生新版本）。
- 上传会话按 user_id 归属校验，避免跨用户写入/完成他人会话。

工程细节：
- 分片落盘 {upload_id}/{index}.part，写临时文件后原子 rename，防半包分片
- 每片可携带 chunk_md5，服务端校验不过即拒绝（网络位翻转防护）
- 会话 TTL 24h，过期由补偿任务清理目录
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from sqlalchemy import select

from app.config.settings import get_settings
from app.core.database import session_scope
from app.core.exceptions import BizError, NotFoundError, ParamError
from app.core.logging import get_logger
from app.infra.cache import get_cache
from app.infra.file_storage import get_file_store
from app.models.entities import DocumentVersion, KnowledgeDocument
from app.models.queries import base_filter
from app.modules.document.entities_meta import next_version_from
from app.modules.document.events import DocumentUploaded
from app.modules.document.file_type import detect_type
from app.modules.document.schemas import ChunkFinalizeRequest, ChunkInitRequest

logger = get_logger("app.upload")

_storage_cfg = get_settings().storage
# 多实例部署时上传请求可能落到任意实例，会话目录必须共享；
# STORAGE__UPLOAD_ROOT 未配置时沿用本地默认目录（单机语义不变）
_SESSION_ROOT = Path(_storage_cfg.upload_root or Path(_storage_cfg.root) / "upload_sessions")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_DONE_PREFIX = "ke:upload:done"
_UNSUPPORTED_TYPES = {"unknown", "doc_legacy"}


class UploadSessionStore:
    """上传会话仓储（文件系统实现：元数据 JSON + 分片目录）。"""

    def __init__(self):
        _SESSION_ROOT.mkdir(parents=True, exist_ok=True)

    def _dir(self, upload_id: str) -> Path:
        return _SESSION_ROOT / upload_id

    def create(self, meta: dict) -> Path:
        d = self._dir(meta["upload_id"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return d

    def load(self, upload_id: str) -> dict | None:
        f = self._dir(upload_id) / "meta.json"
        if not f.exists():
            return None
        return json.loads(f.read_text(encoding="utf-8"))

    def save(self, meta: dict) -> None:
        (self._dir(meta["upload_id"]) / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def uploaded_indexes(self, upload_id: str) -> list[int]:
        d = self._dir(upload_id)
        if not d.exists():
            return []
        return sorted(int(p.name.split(".")[0]) for p in d.glob("*.part"))

    def remove(self, upload_id: str) -> None:
        shutil.rmtree(self._dir(upload_id), ignore_errors=True)

    def expired_ids(self, ttl_seconds: int) -> list[str]:
        now = time.time()
        result = []
        for d in _SESSION_ROOT.iterdir():
            meta_file = d / "meta.json"
            if not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                if now - meta.get("created_ts", now) > ttl_seconds:
                    result.append(d.name)
            except json.JSONDecodeError:
                result.append(d.name)   # 元数据损坏的目录一并回收
        return result


_store = UploadSessionStore()


def done_marker_key(file_md5: str) -> str:
    """秒传标记 key（删除服务复用同一规则清理标记）。"""
    return f"{_DONE_PREFIX}:{file_md5.lower()}"


class ChunkedUploadService:
    # ---------------- 1. 初始化 ----------------
    async def init(self, req: ChunkInitRequest, user_id: str) -> dict:
        cfg = get_settings().storage
        if req.file_size > cfg.max_upload_bytes:
            raise ParamError(f"文件超过上限 {cfg.max_upload_bytes // 1024 // 1024}MB")
        if not _HEX_RE.match(req.file_md5):
            raise ParamError("file_md5 必须为 32 位十六进制")

        chunk_size = req.chunk_size or cfg.chunk_size
        chunk_size = max(min(chunk_size, 50 * 1024 * 1024), 64 * 1024)   # 夹到 [64KB, 50MB]
        total_chunks = (req.file_size + chunk_size - 1) // chunk_size

        # upload_id 由 md5+用户 派生：同文件同用户天然复用（断点续传幂等）
        upload_id = hashlib.sha256(f"{req.file_md5}:{user_id}".encode()).hexdigest()[:32]

        # 上传新版本：先校验目标文档存在，且绝不允许秒传（同内容也要产生新版本）
        if req.doc_id is not None:
            await self._assert_doc_exists(req.doc_id)
        else:
            hit = await self._resolve_instant_hit(req.file_md5)
            if hit is not None:
                logger.info("instant_upload_hit", md5=req.file_md5, user=user_id,
                            doc_id=hit["doc_id"], version_id=hit["version_id"])
                return {"instant_hit": True, "chunk_size": chunk_size,
                        "uploaded_chunks": [], "doc_id": hit["doc_id"],
                        "version_id": hit["version_id"], "doc_url": hit.get("doc_url")}

        meta = {
            "upload_id": upload_id, "file_md5": req.file_md5, "file_name": req.file_name,
            "file_size": req.file_size, "chunk_size": chunk_size, "total_chunks": total_chunks,
            "user_id": user_id, "doc_id": req.doc_id, "changelog": req.changelog,
            "created_ts": time.time(),
        }
        _store.create(meta)
        return {
            "instant_hit": False,
            "upload_id": upload_id,
            "chunk_size": chunk_size,
            # 断点续传：返回已收分片，前端只补差集
            "uploaded_chunks": _store.uploaded_indexes(upload_id),
        }

    async def _resolve_instant_hit(self, file_md5: str) -> dict | None:
        """校验秒传标记指向的文档/版本是否仍存活；失效标记立即清除。"""
        raw = await get_cache().get_raw(done_marker_key(file_md5))
        if not raw:
            return None
        try:
            info = json.loads(raw) if isinstance(raw, str) else dict(raw)
            doc_id, version_id = int(info["doc_id"]), int(info["version_id"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            # 旧格式（仅存 doc_url 字符串）或脏数据：直接作废
            await get_cache().delete(done_marker_key(file_md5))
            return None
        if not await self._target_alive(doc_id, version_id):
            await get_cache().delete(done_marker_key(file_md5))
            return None
        return {"doc_id": doc_id, "version_id": version_id, "doc_url": info.get("doc_url")}

    @staticmethod
    async def _assert_doc_exists(doc_id: int) -> None:
        async with session_scope() as session:
            doc = (await session.execute(
                base_filter(KnowledgeDocument).where(KnowledgeDocument.doc_id == doc_id)
            )).scalar_one_or_none()
        if doc is None:
            raise NotFoundError("目标文档不存在或已删除，无法上传新版本")

    @staticmethod
    async def _target_alive(doc_id: int, version_id: int) -> bool:
        async with session_scope() as session:
            doc = (await session.execute(
                base_filter(KnowledgeDocument).where(KnowledgeDocument.doc_id == doc_id)
            )).scalar_one_or_none()
            if doc is None:
                return False
            ver = (await session.execute(
                base_filter(DocumentVersion).where(
                    DocumentVersion.version_id == version_id,
                    DocumentVersion.doc_id == doc_id)
            )).scalar_one_or_none()
        return ver is not None

    # ---------------- 2. 上传分片 ----------------
    async def put_chunk(self, upload_id: str, index: int, data: bytes,
                        user_id: str, chunk_md5: str | None = None) -> dict:
        meta = self._load_owned(upload_id, user_id)
        if not 0 <= index < meta["total_chunks"]:
            raise ParamError(f"分片序号越界: {index}")
        expected = meta["chunk_size"] if index < meta["total_chunks"] - 1 \
            else meta["file_size"] - index * meta["chunk_size"]
        if len(data) != expected:
            raise ParamError(f"分片大小不符（期望 {expected}，实际 {len(data)}），请重传该分片")
        if chunk_md5 and hashlib.md5(data).hexdigest() != chunk_md5.lower():
            raise ParamError(f"分片 {index} MD5 校验失败，请重传")

        # 原子写入：先写 .tmp 再 rename，防并发读到半包文件
        dest = _store._dir(upload_id) / f"{index}.part"
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return {"uploaded": index, "received": len(_store.uploaded_indexes(upload_id)),
                "total": meta["total_chunks"]}

    # ---------------- 3. 进度对账 ----------------
    async def status(self, upload_id: str, user_id: str) -> dict:
        meta = self._load_owned(upload_id, user_id)
        uploaded = _store.uploaded_indexes(upload_id)
        return {"upload_id": upload_id, "uploaded_chunks": uploaded,
                "total_chunks": meta["total_chunks"],
                "percent": round(len(uploaded) / max(meta["total_chunks"], 1) * 100, 1)}

    # ---------------- 4. 合并完成 ----------------
    async def finalize(self, req: ChunkFinalizeRequest, user_id: str) -> dict:
        meta = self._load_owned(req.upload_id, user_id)
        if meta.get("doc_id"):
            await self._assert_doc_exists(int(meta["doc_id"]))

        uploaded = set(_store.uploaded_indexes(req.upload_id))
        missing = [i for i in range(meta["total_chunks"]) if i not in uploaded]
        if missing:
            raise BizError(f"尚有 {len(missing)} 个分片未上传（如 {missing[:5]}），请续传后重试")

        # 流式合并 + 全文 MD5 校验（不把大文件整个读进内存）
        merged = _store._dir(req.upload_id) / "merged.bin"
        h = hashlib.md5()
        size = 0
        with merged.open("wb") as out:
            for i in range(meta["total_chunks"]):
                part = _store._dir(req.upload_id) / f"{i}.part"
                chunk = part.read_bytes()
                h.update(chunk)
                size += len(chunk)
                out.write(chunk)
        actual_md5 = h.hexdigest()
        if actual_md5 != meta["file_md5"].lower() or size != meta["file_size"]:
            merged.unlink(missing_ok=True)
            _store.remove(req.upload_id)   # 内容已损坏，强制整个会话重传
            raise BizError("文件完整性校验失败（MD5/大小不符），会话已作废，请重新上传")

        # 类型检测：二进制看魔数，文本类回传原始文件名（merged.bin 已丢失扩展名）辅助判定
        file_type = detect_type(merged, meta["file_name"])
        if file_type in _UNSUPPORTED_TYPES:
            merged.unlink(missing_ok=True)
            _store.remove(req.upload_id)
            if file_type == "doc_legacy":
                raise ParamError("暂不支持旧版 Word（.doc），请另存为 .docx 后重新上传")
            raise ParamError("无法识别的文件类型，仅支持 PDF/Word/Excel/CSV/Markdown/TXT")

        # 入对象存储
        doc_url = await get_file_store().put(get_settings().storage.bucket,
                                             meta["file_name"], merged)

        # 建文档/版本：失败时清理已入库对象，但保留上传会话以便重试（不再提前作废会话）
        try:
            doc_id, version_id = await self._create_document(
                req, meta, doc_url, file_type, user_id)
        except Exception:
            try:
                await get_file_store().delete(doc_url)
            except Exception as cleanup_error:   # pragma: no cover - 清理失败不影响原异常
                logger.warning("upload_cleanup_failed", doc_url=doc_url, error=str(cleanup_error))
            raise

        _store.remove(req.upload_id)
        # 秒传标记必须在建库成功之后写，避免失败留下"毒标记"
        await self._remember_done(meta["file_md5"], doc_id, version_id, doc_url)
        return {"doc_id": doc_id, "version_id": version_id, "file_type": file_type,
                "doc_url": doc_url}

    def _load_owned(self, upload_id: str, user_id: str) -> dict:
        meta = _store.load(upload_id)
        # 不区分"不存在"与"无权限"，避免通过 upload_id 探测他人会话
        if meta is None or meta.get("user_id") != user_id:
            raise NotFoundError("上传会话不存在、已过期或无权限访问")
        return meta

    @staticmethod
    async def _remember_done(file_md5: str, doc_id: int, version_id: int, doc_url: str) -> None:
        await get_cache().set_raw(done_marker_key(file_md5), json.dumps({
            "doc_id": doc_id, "version_id": version_id, "doc_url": doc_url,
        }, ensure_ascii=False), ttl=7 * 86400)

    async def _create_document(self, req: ChunkFinalizeRequest, meta: dict,
                               doc_url: str, file_type: str, user_id: str) -> tuple[int, int]:
        split_conf = req.model_dump(exclude={"upload_id"})
        md5 = meta["file_md5"].lower()
        async with session_scope() as session:
            if meta.get("doc_id"):
                doc = (await session.execute(
                    base_filter(KnowledgeDocument)
                    .where(KnowledgeDocument.doc_id == int(meta["doc_id"])))).scalar_one_or_none()
                if doc is None:
                    raise NotFoundError("目标文档不存在，无法创建新版本")
                # 版本号基于全部历史版本（含软删），避免撞 uk_doc_version 唯一键
                history = (await session.execute(
                    select(DocumentVersion.version)
                    .where(DocumentVersion.doc_id == doc.doc_id))).scalars().all()
                version = next_version_from(list(history))
                ver = DocumentVersion(doc_id=doc.doc_id, version=version, doc_url=doc_url,
                                      status="UPLOADED", upload_user=user_id,
                                      changelog=meta.get("changelog"))
                session.add(ver)
                await session.flush()
                doc.status = "UPLOADED"
                doc.current_version_id = ver.version_id
                doc.extension = _merge_extension(
                    doc.extension, version_id=ver.version_id, version=version,
                    split_conf=split_conf, file_type=file_type, md5=md5)
                doc_id, version_id = doc.doc_id, ver.version_id
            else:
                doc = KnowledgeDocument(doc_title=meta["file_name"], status="UPLOADED",
                                        knowledge_base_type=req.knowledge_base_type,
                                        accessible_by=req.accessible_by,
                                        description=req.description, expire_date=req.expire_date)
                session.add(doc)
                await session.flush()
                ver = DocumentVersion(doc_id=doc.doc_id, version="1.0.0", doc_url=doc_url,
                                      status="UPLOADED", upload_user=user_id,
                                      changelog=meta.get("changelog"))
                session.add(ver)
                await session.flush()
                doc.current_version_id = ver.version_id
                doc.extension = _merge_extension(
                    doc.extension, version_id=ver.version_id, version="1.0.0",
                    split_conf=split_conf, file_type=file_type, md5=md5)
                doc_id, version_id = doc.doc_id, ver.version_id

        from app.infra.event_bus import get_event_bus
        get_event_bus().publish(DocumentUploaded(doc_id=doc_id, version_id=version_id))
        logger.info("upload_finalized", doc_id=doc_id, version_id=version_id, type=file_type)
        return doc_id, version_id

    # ---------------- 会话清理（补偿任务调用） ----------------
    def sweep_expired(self) -> int:
        ttl = get_settings().storage.upload_session_ttl_seconds
        removed = 0
        for upload_id in _store.expired_ids(ttl):
            _store.remove(upload_id)
            removed += 1
        if removed:
            logger.info("upload_sessions_swept", count=removed)
        return removed


def _merge_extension(extension: dict | None, *, version_id: int, version: str,
                     split_conf: dict, file_type: str, md5: str) -> dict:
    """增量合并文档扩展信息，保留历史阶段的 md5 / 流水线轨迹，绝不整块覆盖。"""
    ext = dict(extension or {})
    ext["splitConfig"] = split_conf
    ext["fileType"] = file_type
    ext["version"] = version
    md5s = [str(x).lower() for x in (ext.get("fileMd5s") or []) if x]
    if md5 not in md5s:
        md5s.append(md5)
    ext["fileMd5s"] = md5s[-50:]
    versions = dict(ext.get("versions") or {})
    versions[str(version_id)] = {
        "stages": [], "attempts": 0, "md5": md5, "version": version,
    }
    ext["versions"] = versions
    return ext


_service: ChunkedUploadService | None = None


def get_upload_service() -> ChunkedUploadService:
    global _service
    if _service is None:
        _service = ChunkedUploadService()
    return _service
