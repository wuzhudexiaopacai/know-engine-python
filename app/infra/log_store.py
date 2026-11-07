"""日志集中存储与检索（Elasticsearch）。

设计目标（多机/多副本部署）：
- 每个实例把结构化日志推送到同一个 ES 日志索引，运维在「日志中心」一处检索，
  而不是逐个 `docker logs` 翻；
- 写入走「有界队列 + 后台线程 + 批量 bulk」，业务线程只做一次内存入队，
  ES 慢/挂/队列满时只丢弃日志（并计数），绝不拖慢请求；
- 索引按月创建（`know-engine-logs-YYYY.MM`），字段映射固定，方便聚合与保留策略；
- 不用新增日志容器：直接复用向量检索用的 Elasticsearch。

注意：本模块被 logging 初始化时导入，内部日志一律走标准库 `logging`，
避免与 structlog 处理链互相递归。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any

from app.config.settings import get_settings

_worker_logger = logging.getLogger("app.log_store")

# 索引字段映射：只声明稳定字段，其余动态字段交给 ES 自动推断
_LOG_MAPPINGS = {
    "properties": {
        "@timestamp": {"type": "date"},
        "level": {"type": "keyword"},
        "event": {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}},
        "logger": {"type": "keyword"},
        "service": {"type": "keyword"},
        "env": {"type": "keyword"},
        "host": {"type": "keyword"},
        "instance": {"type": "keyword"},
        "pid": {"type": "integer"},
        "request_id": {"type": "keyword"},
        "tenant_id": {"type": "long"},
        "account_id": {"type": "keyword"},
        "identity": {"type": "keyword"},
        "module": {"type": "keyword"},
        "action": {"type": "keyword"},
        "path": {"type": "keyword"},
        "method": {"type": "keyword"},
        "status_code": {"type": "integer"},
        "duration_ms": {"type": "integer"},
        "client_ip": {"type": "keyword"},
    }
}
_LOG_SETTINGS = {"number_of_shards": 1, "number_of_replicas": 0}

_client_kwargs_cache: dict[str, Any] | None = None


def _es_kwargs() -> dict[str, Any]:
    """复用向量检索的 ES 连接配置（地址/账号密码/超时）。"""
    global _client_kwargs_cache
    if _client_kwargs_cache is not None:
        return _client_kwargs_cache
    cfg = get_settings().vector
    kwargs: dict[str, Any] = {"hosts": cfg.es_url, "request_timeout": 3.0,
                              "max_retries": 2, "retry_on_timeout": False}
    if cfg.es_username:
        kwargs["basic_auth"] = (cfg.es_username, cfg.es_password)
    _client_kwargs_cache = kwargs
    return kwargs


def _month_index(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y.%m')}"


class ElasticsearchLogHandler(logging.Handler):
    """把 JSON 日志批量写入 ES 的 Handler（线程安全、永不阻塞调用方）。"""

    def __init__(self, *, index_prefix: str, queue_size: int, bulk_size: int,
                 flush_interval: float) -> None:
        super().__init__()
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._index_prefix = index_prefix
        self._bulk_size = bulk_size
        self._flush_interval = flush_interval
        self._client = None
        self._created_indices: set[str] = set()
        self._dropped = 0
        self._fail_streak = 0
        self._last_fail_log = 0.0
        self._backoff_until = 0.0
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ke-log-shipper", daemon=True)
        self._thread.start()

    # ---------------- Handler 接口 ----------------
    def emit(self, record: logging.LogRecord) -> None:
        # 后台线程自身 / 已被关闭时不再入队，防递归与关闭后写入
        if self._stop_event.is_set() or record.name.startswith("app.log_store"):
            return
        try:
            payload = json.loads(self.format(record))
        except Exception:
            return
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self._dropped += 1
            # 只周期性提示，避免 ES 故障时刷屏
            if self._dropped in (1, 100, 1000, 10000) or self._dropped % 10000 == 0:
                _worker_logger.warning("log_queue_full dropped=%s", self._dropped)

    def close(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)      # 唤醒并退出后台线程
        except queue.Full:
            pass
        self._thread.join(timeout=3.0)
        self._close_client()
        super().close()

    # ---------------- 后台批量发送 ----------------
    def _connect(self):
        if self._client is not None:
            return self._client
        from elasticsearch import Elasticsearch

        self._client = Elasticsearch(**_es_kwargs())
        return self._client

    def _close_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:             # pragma: no cover
                pass
            self._client = None

    def _ensure_index(self, client, index: str) -> None:
        if index in self._created_indices:
            return
        if not client.indices.exists(index=index):
            try:
                client.indices.create(
                    index=index,
                    settings=_LOG_SETTINGS,
                    mappings=_LOG_MAPPINGS,
                )
            except Exception as exc:
                # 并发实例同时创建 → resource_already_exists_exception，属正常竞争
                if "resource_already_exists_exception" not in str(exc):
                    raise
        self._created_indices.add(index)

    def _flush_batch(self, records: list[dict]) -> None:
        index = _month_index(self._index_prefix)
        client = self._connect()
        self._ensure_index(client, index)
        operations: list[dict] = []
        for doc in records:
            doc.pop("_record", None)
            doc.pop("_from_structlog", None)
            operations.append({"index": {"_index": index}})
            operations.append(doc)
        client.bulk(operations=operations, refresh=False)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                first = self._queue.get(timeout=self._flush_interval)
                if first is None:
                    break
                batch = [first]
                deadline = time.monotonic() + 0.5
                while len(batch) < self._bulk_size and time.monotonic() < deadline:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        self._stop_event.set()
                        break
                    batch.append(item)
                # ES 连续失败后进入退避窗口：直接丢弃本批，避免反复超时拖住线程
                if time.monotonic() < self._backoff_until:
                    continue
                try:
                    self._flush_batch(batch)
                    self._fail_streak = 0
                    self._last_fail_log = 0.0
                except Exception as exc:
                    # ES 故障：关闭连接，进入指数退避；本地文件仍留档
                    self._fail_streak += 1
                    self._close_client()
                    self._created_indices.clear()
                    delay = min(60.0, 5.0 * (2 ** (self._fail_streak - 1)))
                    self._backoff_until = time.monotonic() + delay
                    now = time.monotonic()
                    if self._fail_streak == 1 or now - self._last_fail_log > 60:
                        self._last_fail_log = now
                        _worker_logger.warning("log_es_flush_failed count=%s error=%s",
                                               len(batch), str(exc)[:200])
            except Exception as exc:      # pragma: no cover - 后台线程兜底
                _worker_logger.warning("log_worker_error error=%s", str(exc)[:200])
                time.sleep(0.5)
        # 退出前尽量把剩余日志写完
        self._drain_remaining()

    def _drain_remaining(self) -> None:
        remaining: list[dict] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                remaining.append(item)
        if remaining:
            try:
                self._flush_batch(remaining)
            except Exception:             # pragma: no cover
                pass


# ------------------------------------------------------------------
# 查询（日志中心「系统日志」Tab）
# ------------------------------------------------------------------
def es_logging_enabled() -> bool:
    cfg = get_settings()
    return bool(cfg.logging.es_enabled and cfg.vector.es_url)


def _iso_local(value: str) -> str:
    """前端传本地时间（不带时区）→ 带本地时区偏移的 ISO8601，避免 ES 按 UTC 解析。"""
    text = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.astimezone().isoformat()
        except ValueError:
            continue
    return value          # 已带时区的输入原样透传


def _build_query(keyword: str | None, level: str | None, request_id: str | None,
                 tenant_id: int | None, module: str | None,
                 start: str | None, end: str | None) -> dict:
    must: list[dict] = []
    filters: list[dict] = []
    if keyword:
        must.append({"multi_match": {
            "query": keyword,
            "fields": ["event^3", "path", "logger", "action", "module"],
            "type": "best_fields",
        }})
    if level:
        filters.append({"term": {"level": level.upper()}})
    if request_id:
        filters.append({"term": {"request_id": request_id}})
    if tenant_id is not None:
        filters.append({"term": {"tenant_id": tenant_id}})
    if module:
        filters.append({"term": {"module": module}})
    if start or end:
        rng: dict[str, str] = {}
        if start:
            rng["gte"] = _iso_local(start)
        if end:
            rng["lte"] = _iso_local(end)
        filters.append({"range": {"@timestamp": rng}})
    query: dict = {"bool": {"must": must or [{"match_all": {}}], "filter": filters}}
    return {"query": query, "sort": (["_score", {"@timestamp": "desc"}] if keyword
                                     else [{"@timestamp": "desc"}]),
            "track_total_hits": True,
            "size": 0}   # size 由调用方覆写


async def search_system_logs(*, page: int, size: int, keyword: str | None = None,
                             level: str | None = None, request_id: str | None = None,
                             tenant_id: int | None = None, module: str | None = None,
                             start: str | None = None, end: str | None = None) -> dict:
    """查询 ES 日志索引；ES 未配置/不可用时返回 degraded，不影响页面其他功能。"""
    cfg = get_settings()
    if not es_logging_enabled():
        return {"degraded": True, "reason": "未配置 Elasticsearch，当前仅保留本地日志文件",
                "backend": "file", "total": 0, "records": []}

    from elasticsearch import AsyncElasticsearch

    client = AsyncElasticsearch(**_es_kwargs())
    index = f"{cfg.logging.es_index_prefix}-*"
    body = _build_query(keyword, level, request_id, tenant_id, module, start, end)
    body["from_"] = max(0, (page - 1) * size)
    body["size"] = min(size, cfg.logging.query_max_size)
    try:
        resp = await client.search(index=index, **body)
    except Exception as exc:
        if "index_not_found_exception" in str(exc):
            return {"degraded": False, "backend": "elasticsearch", "total": 0, "records": []}
        return {"degraded": True, "reason": f"Elasticsearch 查询失败：{str(exc)[:200]}",
                "backend": "elasticsearch", "total": 0, "records": []}
    finally:
        await client.close()

    hits = resp.get("hits", {})
    total = int(hits.get("total", {}).get("value", 0)) if isinstance(hits.get("total"), dict) \
        else int(hits.get("total", 0))
    records = []
    for hit in hits.get("hits", []):
        src = hit.get("_source", {})
        src["_id"] = hit.get("_id")
        records.append(src)
    return {"degraded": False, "backend": "elasticsearch",
            "total": total, "records": records}
