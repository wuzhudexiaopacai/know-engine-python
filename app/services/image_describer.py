"""文档内图片理解：上传对象存储 + 轻量视觉模型生成一句话描述。

设计取舍：
- 视觉模型来自后台「模型配置」中 purpose=vision 的启用项，可单独配置轻量模型，
  与对话主模型完全解耦；未配置时只上传图片并用位置文案做 alt，绝不阻塞文档处理
- 同一张图按 SHA-256 去重：重复的 logo / 水印只描述一次，命中进程内缓存直接复用
- 并发受信号量约束，单图超时后降级为位置文案；任何一张失败都不影响切片与向量化
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.infra.file_storage import get_file_store
from app.modules.document.converter import ExtractedImage
from app.modules.model import service as model_service
from app.services.llm_client import LlmClient, ModelCapabilityError, get_model_llm

logger = get_logger("app.image_describer")

_PROMPT = (
    "请用一句简洁的中文说明这张图片的内容。"
    "若是图表/流程图/表格截图，说明其类型和关键信息；"
    "若是照片或界面截图，描述主要对象与场景。"
    "只输出描述本身，不要以「这张图片」开头，不要编造看不清的内容，不要输出 Markdown。"
)

_MIME_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/bmp": ".bmp", "image/tiff": ".tiff",
}

# 进程内描述缓存：同一张图片（如公司 logo）在后续文档中直接复用，避免重复调用视觉模型
_DESC_CACHE: OrderedDict[str, str] = OrderedDict()
_CACHE_MAX = 500

# 「已确认不支持图片输入」的模型标记（端点:模型 → 过期时间）。
# 命中后整篇文档直接跳过描述，避免每张图都打一次 400、白等超时。
_UNSUPPORTED_TTL = 600.0
_UNSUPPORTED_MODELS: dict[str, float] = {}


@dataclass
class DescribedImage:
    """描述完成后的一张图片：可直接用于替换 Markdown 占位符。"""

    marker: str
    url: str
    alt: str
    label: str
    description: str | None      # None = 未生成描述（无视觉模型/超时/失败）

    @property
    def described(self) -> bool:
        return bool(self.description)


@dataclass
class DescribeOutcome:
    """一批图片的处理结果 + 降级原因（原因会写进阶段详情，便于后台排查）。"""

    images: list[DescribedImage]
    note: str = ""

    def __iter__(self):
        return iter(self.images)

    def __len__(self) -> int:
        return len(self.images)


async def prepare_images(images: list[ExtractedImage]) -> DescribeOutcome:
    """上传图片并生成描述；返回可直接写回 Markdown 的结果列表。

    视觉模型不可用（未配置 / 不支持图片输入）时只上传不描述，note 说明原因，
    整篇文档不会因为「模型选错」而反复打 400 或触发熔断。
    """
    cfg = get_settings().image
    if not images:
        return DescribeOutcome([])
    selected = [im for im in images
                if cfg.min_image_bytes <= len(im.data) <= cfg.max_image_bytes][: cfg.max_images]
    if not selected:
        return DescribeOutcome([])

    store = get_file_store()
    client = await _vision_client() if cfg.parse_enabled else None
    note = ""
    if client is None:
        note = "未配置图片理解模型，图片仅保留位置"
    elif _unsupported_cached(client.signature):
        note = f"图片理解模型（{client.chat_model}）不支持图片输入，已跳过描述"
        logger.warning("vision_model_unsupported_cached", model=client.chat_model)
        client = None
    semaphore = asyncio.Semaphore(max(1, cfg.describe_concurrency))
    uploaded: dict[str, str] = {}
    # 一旦确认模型不支持图片输入，立刻让排队中的图片直接跳过（不再逐张失败）
    stop = asyncio.Event()

    async def upload(im: ExtractedImage) -> str:
        url = uploaded.get(im.sha256)
        if url:
            return url
        key = await store.put(
            "doc-images", f"{im.sha256[:16]}{_MIME_EXT.get(im.mime, '.png')}", im.data)
        # 固定回读路由而非预签名 URL：Markdown 会长期保留在知识库中，预签名会过期
        url = f"/api/files/{key}"
        uploaded[im.sha256] = url
        return url

    async def describe(im: ExtractedImage) -> str | None:
        nonlocal note
        if client is None or stop.is_set():
            return None
        cached = _cache_get(im.sha256)
        if cached:
            return cached
        async with semaphore:
            if stop.is_set():
                return None
            try:
                async with asyncio.timeout(cfg.describe_timeout_seconds):
                    raw = await client.chat_messages(
                        _vision_messages(im), max_tokens=200, temperature=0.2)
            except ModelCapabilityError as e:
                # 配置问题：标记该模型，10 分钟内不再尝试，避免每篇文档重复踩坑
                _mark_unsupported(client.signature)
                note = (f"图片理解模型（{client.chat_model}）不可用：{e}，"
                        "请检查后台「模型配置」")
                stop.set()
                logger.error("vision_model_unsupported", model=client.chat_model,
                             error=str(e))
                return None
            except Exception as e:
                logger.warning("image_describe_failed", label=im.label, error=str(e))
                return None
        text = _clean_description(raw)
        if text:
            _cache_put(im.sha256, text)
            return text
        return None

    async def one(im: ExtractedImage) -> DescribedImage:
        url = await upload(im)
        description = await describe(im)
        return DescribedImage(
            marker=im.marker, url=url, label=im.label, description=description,
            alt=description or f"图片（{im.label}）")

    return DescribeOutcome(list(await asyncio.gather(*(one(im) for im in selected))), note)


async def _vision_client() -> LlmClient | None:
    runtime = await model_service.resolve_vision_runtime()
    if runtime is None:
        logger.info("vision_model_not_configured")
        return None
    return get_model_llm(runtime.id, runtime.lock_version, runtime.base_url,
                         runtime.api_key, runtime.model_name, runtime.allow_empty_key)


def _mark_unsupported(signature: str) -> None:
    _UNSUPPORTED_MODELS[signature] = time.monotonic() + _UNSUPPORTED_TTL


def _unsupported_cached(signature: str) -> bool:
    expire = _UNSUPPORTED_MODELS.get(signature)
    if expire is None:
        return False
    if time.monotonic() > expire:
        _UNSUPPORTED_MODELS.pop(signature, None)
        return False
    return True


def _vision_messages(im: ExtractedImage) -> list[dict]:
    data_url = f"data:{im.mime};base64,{base64.b64encode(im.data).decode('ascii')}"
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": f"{_PROMPT}\n图片位置：{im.label}"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }]


def _clean_description(text: str) -> str:
    """一句话化 + Markdown 安全（alt 中出现的方括号会破坏图片语法）。"""
    text = re.sub(r"\s+", " ", text or "").strip().strip("`\"' ")
    text = text.replace("[", "【").replace("]", "】")
    return text[:200]


def _cache_get(sha: str) -> str | None:
    value = _DESC_CACHE.get(sha)
    if value is not None:
        _DESC_CACHE.move_to_end(sha)
    return value


def _cache_put(sha: str, text: str) -> None:
    _DESC_CACHE[sha] = text
    _DESC_CACHE.move_to_end(sha)
    while len(_DESC_CACHE) > _CACHE_MAX:
        _DESC_CACHE.popitem(last=False)
