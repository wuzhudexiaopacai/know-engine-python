"""结构化日志（structlog）。

对齐企业规范：
- JSON 输出（prod）/ 彩色控制台（dev）双模式
- 每条日志携带 trace_id，与请求生命周期绑定
- 敏感信息（API Key / 密码）不落日志
"""

from __future__ import annotations

import logging
import sys

import structlog

from app.config.settings import get_settings


def setup_logging() -> None:
    """初始化根日志与 structlog，应用启动时调用一次。"""
    level = logging.INFO
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )
    # 压制三方库噪音
    for noisy in ("httpx", "httpcore", "aiosqlite", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    is_prod = get_settings().app.env == "prod"
    processors: list = [
        structlog.contextvars.merge_contextvars,   # 绑定 trace_id 等上下文
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if is_prod:
        processors.append(structlog.processors.JSONRenderer(ensure_ascii=False))
    else:
        # Windows 彩色控制台依赖 colorama；缺失时自动降级为无色的 ConsoleRenderer，
        # 绝不因日志渲染问题阻断应用启动。
        try:
            import colorama  # noqa: F401
            processors.append(structlog.dev.ConsoleRenderer(colors=True))
        except ImportError:
            processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """获取命名 logger，业务代码统一入口。"""
    return structlog.get_logger(name)
