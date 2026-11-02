"""文生图服务（策略模式：OpenAI 兼容 SDK / DashScope 原生异步任务）。

- OpenAI 兼容端点（OpenAI、智谱 CogView 等）：openai SDK `images.generate`
- DashScope 原生端点（/api/v1）：异步提交 task + 轮询（OpenAI 协议无法表达）

失败/超时/未开通均收敛为 ImageError（可读信息），由接口层降级提示。
"""

from __future__ import annotations

import asyncio

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.services.model_transports import (
    ImageCancelledError as ImageCancelled,
    ImageGenerationError as ImageError,
    select_image_transport,
)

logger = get_logger("app.image")


class ImageService:
    def __init__(self):
        cfg = get_settings().image
        self.cfg = cfg
        self._base = cfg.base_url.rstrip("/")

    @property
    def api_key(self) -> str:
        """独立 Key 优先，留空复用 LLM Key（同一 DashScope 账号）。"""
        return self.cfg.api_key or get_settings().llm.api_key

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def close(self) -> None:
        """传输客户端按请求创建并关闭，此处无需释放资源。"""

    async def generate(self, prompt: str, *, size: str | None = None,
                       n: int = 1,
                       stop_event: asyncio.Event | None = None) -> list[str]:
        """提示词 → 图片 URL 列表。

        stop_event 置位时抛 ImageCancelled：停止轮询、不返回图片（远端任务结果丢弃）。
        """
        base_url, api_key, model, provider = await self._resolve()
        if not api_key:
            raise ImageError("文生图未配置（缺少 API Key）")
        if not prompt.strip():
            raise ImageError("请先描述想生成的画面")
        transport = select_image_transport(
            base_url, provider=provider, model=model,
            poll_timeout_seconds=self.cfg.poll_timeout_seconds,
            submit_timeout_seconds=self.cfg.submit_timeout_seconds)
        try:
            return await transport.generate(
                base_url=base_url, api_key=api_key, model=model,
                prompt=prompt, size=size or self.cfg.default_size, n=n,
                stop_event=stop_event)
        finally:
            await transport.aclose()

    async def _resolve(self) -> tuple[str, str, str, str]:
        """解析当前生效的绘画模型：(配置地址, api_key, 模型名, 提供方)。

        后台「文生图」用途绑定优先；未绑定回退同用途默认模型，再回退环境变量 IMAGE__。
        地址是否原生/兼容由传输层按提供方与模型名折算，这里不再替换用户配置。
        """
        from app.modules.model import service as model_service
        try:
            rt = await model_service.resolve_role_runtime("image_gen")
        except Exception as e:
            logger.warning("image_role_resolve_failed", error=str(e))
            rt = None
        if rt is not None:
            return rt.base_url, rt.api_key, rt.model_name, rt.provider
        return self._base, self.api_key, self.cfg.model, "aliyun"


_service: ImageService | None = None


def get_image() -> ImageService:
    global _service
    if _service is None:
        _service = ImageService()
    return _service


async def close_image() -> None:
    global _service
    _service = None
