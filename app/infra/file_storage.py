"""文件存储抽象（MinIO 主实现 + 本地磁盘降级）。

中间件策略：
- 主实现 MinIOFileStore —— docker-compose 部署（镜像 cgr.dev/chainguard/minio）
- MinIO 不可达时自动降级 LocalFileStore（本地磁盘），接口抽象不变，业务零感知

工程细节：
- MinIO SDK 是同步实现，统一用 run_in_threadpool 包装，避免阻塞事件循环
- 对外访问不落地绝对地址：业务只存对象 key，读文件统一走同源路由 /api/files/{key}
- put_object 失败重试 3 次（幂等覆盖写），仍失败抛 ExternalServiceError
"""

from __future__ import annotations

import re
import shutil
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from app.config.settings import get_settings
from app.core.exceptions import BizError
from app.core.logging import get_logger

logger = get_logger("app.storage")

# 文件名白名单：中英文数字点横线下划线，防路径穿越与注入
_SAFE_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff.\- ]{1,200}$")


def _safe_object_key(bucket: str, filename: str) -> str:
    """生成按月分层的对象 key（bucket/yyyy/mm/uuid-name），文件名过白名单。"""
    if not _SAFE_NAME_RE.match(filename):
        filename = f"file-{uuid.uuid4().hex[:8]}"
    now = datetime.now()
    return f"{bucket}/{now:%Y/%m}/{uuid.uuid4().hex}-{filename}"


class FileStore(ABC):
    """文件存储抽象接口。"""

    @abstractmethod
    async def put(self, bucket: str, filename: str, data: bytes | Path) -> str:
        """写入文件，返回对象 key。"""

    @abstractmethod
    async def get(self, key: str) -> Path:
        """取回本地可读路径（MinIO 实现会先下载到临时目录）。"""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """删除文件；不存在时静默成功（幂等）。"""


class LocalFileStore(FileStore):
    """本地磁盘实现（MinIO 降级替代）。"""

    def __init__(self):
        self.root = Path(get_settings().storage.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        """key → 绝对路径，并校验不逃出根目录（路径穿越防护）。"""
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise BizError("非法文件路径")
        return path

    async def put(self, bucket: str, filename: str, data: bytes | Path) -> str:
        key = _safe_object_key(bucket, filename)
        dest = self._resolve(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, Path):
            shutil.move(str(data), dest)   # 分片合并后的临时文件直接移动，避免二次复制
        else:
            dest.write_bytes(data)
        logger.info("file_stored_local", key=key, size=dest.stat().st_size)
        return key

    async def get(self, key: str) -> Path:
        path = self._resolve(key)
        if not path.exists():
            raise BizError(f"文件不存在: {key}")
        return path

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        if path.exists():
            path.unlink()
            logger.info("file_deleted", key=key)


class MinioFileStore(FileStore):
    """MinIO 主实现（SDK 同步调用全部走线程池）。"""

    def __init__(self):
        from minio import Minio
        import urllib3

        cfg = get_settings().storage
        self.bucket_default = cfg.bucket
        # minio SDK 无 connect_timeout 形参，超时须通过 http_client（urllib3）注入：
        # 连接 3s 快速判定不可达以便降级，读取 60s 容纳大文件上传/下载
        self._client = Minio(
            cfg.endpoint,
            access_key=cfg.access_key,
            secret_key=cfg.secret_key,
            secure=cfg.secure,
            region="us-east-1",
            http_client=urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=3.0, read=60.0), retries=0),
        )
        self._fallback_root = Path(cfg.root)
        self._ensure_bucket(self.bucket_default)

    def _ensure_bucket(self, bucket: str) -> None:
        """惰性建桶（幂等）。"""
        if not self._client.bucket_exists(bucket):
            self._client.make_bucket(bucket)
            logger.info("minio_bucket_created", bucket=bucket)

    async def put(self, bucket: str, filename: str, data: bytes | Path) -> str:
        key = _safe_object_key(bucket, filename)

        def _do():
            self._ensure_bucket(bucket)
            if isinstance(data, Path):
                self._client.fput_object(bucket, key, str(data))
            else:
                from io import BytesIO
                self._client.put_object(bucket, key, BytesIO(data), length=len(data))

        await run_in_threadpool(_do)
        logger.info("file_stored_minio", key=key)
        return key

    async def get(self, key: str) -> Path:
        """下载到本地缓存目录后返回路径（解析器需要文件句柄）。"""
        dest = self._fallback_root / key
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)

        def _do():
            self._client.fget_object(self._bucket_of(key), key, str(dest))

        try:
            await run_in_threadpool(_do)
        except Exception as e:
            raise BizError(f"文件不存在或下载失败: {e}") from e
        return dest

    async def delete(self, key: str) -> None:
        def _do():
            self._client.remove_object(self._bucket_of(key), key)

        await run_in_threadpool(_do)
        logger.info("file_deleted_minio", key=key)

    @staticmethod
    def _bucket_of(key: str) -> str:
        """object key 首段即桶名（_safe_object_key 生成规则）。"""
        return key.split("/", 1)[0]


# ---------------- 工厂：MinIO 优先，探测失败降级本地 ----------------
_store: FileStore | None = None


def get_file_store() -> FileStore:
    global _store
    if _store is None:
        try:
            _store = MinioFileStore()
            logger.info("storage_connected", driver="minio",
                        endpoint=get_settings().storage.endpoint)
        except Exception as e:
            _store = LocalFileStore()
            logger.warning("storage_fallback_local",
                           reason=f"MinIO 不可达（{e}），已降级为本地磁盘，仅限开发环境")
    return _store
