"""文生图服务（DashScope 原生异步任务 API：提交 task + 轮询）。

与 rerank 同为 DashScope 原生协议先例（/api/v1/services/... ）。
流程：
    POST /services/aigc/text2image/image-synthesis（X-DashScope-Async: enable）→ task_id
    GET  /tasks/{task_id} 轮询直至 SUCCEEDED / FAILED（总时限 poll_timeout_seconds）

失败/超时/未开通均收敛为 ImageError（可读信息），由接口层降级提示。
"""

from __future__ import annotations

import asyncio

import httpx

from app.config.settings import get_settings
from app.core.logging import get_logger

logger = get_logger("app.image")

_POLL_INTERVAL = 2.0


class ImageError(Exception):
    """生成失败（携带面向用户的可读信息）。"""


class ImageCancelled(Exception):
    """用户主动中断：轮询边界退出，调用方不落库、不按失败提示。"""


class ImageService:
    def __init__(self):
        cfg = get_settings().image
        self.cfg = cfg
        self._base = cfg.base_url.rstrip("/")
        # 提交用短超时，轮询请求逐次独立（总时限由 generate 控制）
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.submit_timeout_seconds, connect=5.0))

    @property
    def api_key(self) -> str:
        """独立 Key 优先，留空复用 LLM Key（同一 DashScope 账号）。"""
        return self.cfg.api_key or get_settings().llm.api_key

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def close(self) -> None:
        await self._client.aclose()

    async def generate(self, prompt: str, *, size: str | None = None,
                       n: int = 1,
                       stop_event: asyncio.Event | None = None) -> list[str]:
        """提示词 → 图片 URL 列表（DashScope 侧临时地址，需前端及时展示/落库）。

        stop_event 置位时抛 ImageCancelled：停止轮询、不返回图片（远端任务结果丢弃）。
        """
        base_url, api_key, model = await self._resolve()
        if not api_key:
            raise ImageError("文生图未配置（缺少 API Key）")
        if not prompt.strip():
            raise ImageError("请先描述想生成的画面")
        task_id = await self._submit(prompt, size or self.cfg.default_size, n,
                                     base_url, api_key, model)
        return await self._poll(task_id, stop_event, base_url, api_key)

    async def _resolve(self) -> tuple[str, str, str]:
        """解析当前生效的绘画模型：(提交地址, api_key, 模型名)。

        后台「文生图」用途绑定优先；未绑定回退环境变量 IMAGE__ 配置。
        """
        from app.modules.model import service as model_service
        try:
            rt = await model_service.resolve_role_runtime("image_gen")
        except Exception as e:
            logger.warning("image_role_resolve_failed", error=str(e))
            rt = None
        if rt is not None:
            # 模型配置里填原生 API 前缀（含 /api/v1）时直接使用，否则沿用环境变量地址
            base = rt.base_url.rstrip("/")
            if "api/v1" not in base:
                base = self._base
            return base, rt.api_key, rt.model_name
        return self._base, self.api_key, self.cfg.model

    async def _submit(self, prompt: str, size: str, n: int,
                      base_url: str, api_key: str, model: str) -> str:
        try:
            resp = await self._client.post(
                f"{base_url}/services/aigc/text2image/image-synthesis",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json",
                         "X-DashScope-Async": "enable"},
                json={"model": model,
                      "input": {"prompt": prompt},
                      "parameters": {"size": size, "n": max(1, min(n, 4))}},
            )
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            raise ImageError("文生图服务连接失败，请稍后重试") from e
        if resp.status_code >= 400:
            # 403/404 多为模型未开通，400 多为参数问题：直接带原始信息
            raise ImageError(f"文生图提交失败: HTTP {resp.status_code} {resp.text[:200]}")
        output = resp.json().get("output") or {}
        task_id = output.get("task_id")
        if not task_id:
            raise ImageError("文生图提交异常：未返回任务 ID")
        return task_id

    async def _poll(self, task_id: str, stop_event: asyncio.Event | None = None,
                    base_url: str | None = None, api_key: str | None = None) -> list[str]:
        deadline = asyncio.get_event_loop().time() + self.cfg.poll_timeout_seconds
        url = f"{(base_url or self._base)}/tasks/{task_id}"
        headers = {"Authorization": f"Bearer {api_key or self.api_key}"}
        while True:
            if stop_event is not None and stop_event.is_set():
                logger.info("image_cancelled", task_id=task_id)
                raise ImageCancelled()
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                resp = await self._client.get(url, headers=headers)
                data = resp.json() if resp.status_code == 200 else {}
            except (httpx.TimeoutException, httpx.ConnectError, ValueError) as e:
                logger.warning("image_poll_retry", task_id=task_id, error=str(e))
                data = {}
            status = (data.get("output") or {}).get("task_status", "")
            if status == "SUCCEEDED":
                results = (data["output"].get("results") or [])
                urls = [r["url"] for r in results if r.get("url")]
                if not urls:
                    raise ImageError("文生图完成但未返回图片地址")
                return urls
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                message = (data["output"].get("message") or "生成失败")[:200]
                raise ImageError(f"文生图失败: {message}")
            if asyncio.get_event_loop().time() > deadline:
                raise ImageError("文生图超时，请稍后重试")


_service: ImageService | None = None


def get_image() -> ImageService:
    global _service
    if _service is None:
        _service = ImageService()
    return _service


async def close_image() -> None:
    global _service
    if _service is not None:
        await _service.close()
        _service = None
