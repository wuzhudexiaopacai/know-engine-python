"""统一日志（structlog）——控制台 + 滚动文件 + ES 集中检索。

企业规范落地：
- 业务代码统一通过 `get_logger()` 输出结构化日志，禁止 print / 裸 logging；
- 日志有两个落地端：
    1. 本地 `logs/know-engine.log`（每日滚动、保留 N 天，JSON Lines，便于留档与排障）；
    2. Elasticsearch 日志索引（多实例汇入同一处，供「日志中心」检索）；
- 每条日志自动携带 service / env / host / pid / instance，多机部署可区分来源；
- 请求上下文（request_id / method / path / client_ip / tenant_id / account_id）由
  `app.core.request_context` 绑定，access 日志与后续业务日志自动带出；
- 敏感字段（密码 / API Key / Token / Authorization）落日志前统一脱敏。

降级策略：文件目录不可写、ES 不可达、队列满，均只丢弃日志，绝不阻塞请求。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any, MutableMapping

import structlog

from app.config.settings import get_settings

_onboard_logger = logging.getLogger("app.bootstrap")

# 敏感键（小写匹配）：命中后值替换为 ***，防止密钥/口令/token 落盘或进 ES
_SENSITIVE_KEYS = {
    "password", "passwd", "pwd", "api_key", "apikey", "token", "access_token",
    "refresh_token", "authorization", "secret", "secret_key", "access_key",
    "cookie", "set-cookie", "credential", "private_key",
}
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def _is_sensitive(key: str) -> bool:
    low = key.lower()
    return any(token in low for token in _SENSITIVE_KEYS)


def redact_sensitive(_logger, _method_name: str, event_dict: MutableMapping[str, Any]
                     ) -> MutableMapping[str, Any]:
    """递归脱敏：敏感键置 ***，字符串中的 Bearer Token 打码。"""
    def clean(value: Any, key: str = "") -> Any:
        if key and _is_sensitive(key):
            return "***"
        if isinstance(value, dict):
            return {k: clean(v, k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v, key) for v in value]
        if isinstance(value, str):
            return _BEARER.sub(r"\1***", value)
        return value

    return {k: clean(v, k) for k, v in event_dict.items()}


def _service_context(_logger, _method_name: str, event_dict: MutableMapping[str, Any]
                     ) -> MutableMapping[str, Any]:
    """补充来源字段：多实例部署时用于区分是哪台机器/哪个进程。"""
    settings = get_settings()
    event_dict.setdefault("service", settings.app.name)
    event_dict.setdefault("env", settings.app.env)
    event_dict.setdefault("host", socket.gethostname())
    event_dict.setdefault("pid", os.getpid())
    # 容器/多副本场景：主机名即实例标识（K8s Pod 名、Compose 容器名均唯一）
    event_dict.setdefault("instance", f"{socket.gethostname()}:{os.getpid()}")
    return event_dict


def _level_from_settings() -> int:
    name = str(get_settings().logging.level).upper()
    return getattr(logging, name, logging.INFO)


def _build_shared_processors() -> list:
    """structlog 与 stdlib 日志共用的处理链。"""
    return [
        structlog.contextvars.merge_contextvars,
        _service_context,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="@timestamp"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_sensitive,
    ]


def _console_renderer():
    """生产态 stdout 输出 JSON（容器采集标准）；开发态彩色控制台。"""
    if get_settings().app.env == "prod":
        return structlog.processors.JSONRenderer(ensure_ascii=False)
    try:
        import colorama  # noqa: F401
        return structlog.dev.ConsoleRenderer(colors=True)
    except ImportError:
        return structlog.dev.ConsoleRenderer(colors=False)


def _install_stdlib_handlers(root: logging.Logger, shared: list) -> None:
    """把控制台 / 滚动文件 / ES 三个 Handler 挂到 root，stdlib 日志也走同一格式。"""
    cfg = get_settings().logging

    # 控制台：容器规范要求 stdout 输出（docker logs / 云平台采集均依赖它）
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _console_renderer(),
        ],
    ))
    root.addHandler(console)

    # 本地滚动文件：JSON Lines，便于 grep / 归档 / 采集器读取
    if cfg.file_enabled:
        try:
            log_dir = Path(cfg.file_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            # 支持 {host}/{pid} 占位：多实例共享卷时各自写独立文件，避免滚动冲突
            file_name = (cfg.file_name
                         .replace("{host}", socket.gethostname())
                         .replace("{pid}", str(os.getpid())))
            file_handler = logging.handlers.TimedRotatingFileHandler(
                filename=str(log_dir / file_name),
                when="midnight",
                interval=1,
                backupCount=cfg.retention_days,
                encoding="utf-8",
                delay=True,
            )
            file_handler.suffix = "%Y-%m-%d"
            file_handler.setFormatter(structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=shared,
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer(ensure_ascii=False),
                ],
            ))
            root.addHandler(file_handler)
        except Exception as exc:      # 只读文件系统 / 权限不足：降级为仅 stdout
            _onboard_logger.warning("log_file_unavailable dir=%s error=%s", cfg.file_dir, exc)

    # ES 集中检索：批量异步写入，失败只丢弃，不阻塞请求
    if cfg.es_enabled and get_settings().vector.es_url:
        try:
            from app.infra.log_store import ElasticsearchLogHandler

            es_handler = ElasticsearchLogHandler(
                index_prefix=cfg.es_index_prefix,
                queue_size=cfg.es_queue_size,
                bulk_size=cfg.es_bulk_size,
                flush_interval=cfg.es_flush_interval,
            )
            es_handler.setFormatter(structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=shared,
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer(ensure_ascii=False),
                ],
            ))
            root.addHandler(es_handler)
        except Exception as exc:      # pragma: no cover - 依赖缺失时仅本地文件
            _onboard_logger.warning("log_es_unavailable error=%s", exc)


def setup_logging() -> None:
    """初始化日志（应用启动时调用一次，幂等）。"""
    root = logging.getLogger()
    level = _level_from_settings()

    shared = _build_shared_processors()

    # 重入保护：重复调用时清空之前挂的 Handler，避免日志重复写入
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:             # pragma: no cover
            pass
    root.setLevel(level)

    _install_stdlib_handlers(root, shared)

    # uvicorn 自带 Handler 会绕过统一格式：全部收敛到 root，避免重复与格式分裂
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    # access 请求日志由 request_context 中间件统一输出，关闭 uvicorn 自带的重复 access 日志
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # 压制三方库噪音
    for noisy in ("httpx", "httpcore", "aiosqlite", "asyncio", "urllib3",
                  "elastic_transport", "elasticsearch"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def shutdown_logging() -> None:
    """停止日志侧后台资源（ES 发送线程），应用关闭时调用。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.__class__.__name__ == "ElasticsearchLogHandler":
            try:
                handler.close()
            except Exception:         # pragma: no cover
                pass
            root.removeHandler(handler)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """获取命名 logger，业务代码统一入口。"""
    return structlog.get_logger(name)
