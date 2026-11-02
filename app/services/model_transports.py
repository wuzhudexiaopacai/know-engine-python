"""模型传输层策略（Strategy 模式）：同一能力可由不同 SDK / 协议实现。

国内企业最常见做法：
- 凡提供 OpenAI 兼容端点的模型（阿里百炼 compatible-mode、智谱、DeepSeek、
  Kimi、本地 vLLM/Ollama）统一走官方 `openai` SDK；
- 只有 OpenAI 协议无法表达的原生异步任务（DashScope 文生图）保留 httpx 直连。

策略族：
- ChatTransport   ｜ chat / vision / memory（openai SDK / http）
- SpeechTransport ｜ 语音转写（Whisper /audio/transcriptions、阿里 Qwen-ASR 对话协议、
                     DashScope 原生多模态生成）
- ImageTransport  ｜ 文生图（openai SDK images / DashScope 原生异步任务）

选择依据：端点协议特征 + SDK 是否可用；SDK 缺失时自动回落 httpx，
与既有 model_protocols（向量化/精排报文协议）正交。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json

from abc import ABC, abstractmethod
from typing import AsyncIterator

import httpx

# ------------------------------------------------------------------
# 统一异常：供 llm_client / speech / image 复用，避免各服务自定义一套
# ------------------------------------------------------------------
class ModelCapabilityError(Exception):
    """模型能力或配置不匹配（如纯文本模型当视觉用）：不重试、不计熔断。"""


class RetryableTransportError(Exception):
    """瞬时依赖故障（超时/连接/429/5xx）：允许指数退避重试。"""


class TransportStatusError(Exception):
    """其余 HTTP 状态错误：原样上报，不重试。"""

    def __init__(self, status_code: int | None, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}" if status_code else detail)


class ImageGenerationError(Exception):
    """文生图失败（携带面向用户的可读信息）。"""


class ImageCancelledError(Exception):
    """用户主动中断：调用方不落库、不按失败提示。"""


_CAPABILITY_MARKS = ("multimodal", "image input",
                     "does not support image", "unsupported image")


def classify_status(status_code: int, body: str) -> None:
    """把 HTTP 状态码归类为「可重试 / 能力不匹配 / 直接失败」。

    与 llm_client 历史语义保持一致：400/404 中的多模态与模型不存在属于配置问题，
    重试和熔断都无意义；401/403 等直接失败并保留原始报文便于排障。
    """
    if 200 <= status_code < 300:
        return
    if status_code in (408, 429, 500, 502, 503, 504):
        raise RetryableTransportError(f"上游依赖抖动: HTTP {status_code}")
    low = (body or "").lower()
    if status_code in (400, 422) and any(m in low for m in _CAPABILITY_MARKS):
        raise ModelCapabilityError("模型不支持图片输入")
    if status_code == 404 and ("not found" in low or "not_found" in low):
        raise ModelCapabilityError("模型不存在或名称错误")
    raise TransportStatusError(status_code, (body or "")[:300].replace("\n", " "))


def sdk_available() -> bool:
    """openai SDK 是否可用（惰性探测，未安装时整条链路回落到 httpx）。"""
    return importlib.util.find_spec("openai") is not None


def _strip_suffix(url: str, suffix: str) -> str:
    base = (url or "").rstrip("/")
    if suffix and base.endswith(suffix):
        base = base[: -len(suffix)]
    return base


def _describe(exc: BaseException) -> str:
    """展开 SDK 包装异常的根因：SDK 只给 "Connection error."，
    真正的 DNS/SSL/连接重置信息在 __cause__ 链里。"""
    root = exc
    seen = 0
    while getattr(root, "__cause__", None) is not None and seen < 5:
        root = root.__cause__
        seen += 1
    if root is exc:
        return f"{type(exc).__name__}: {exc}"
    return f"{type(exc).__name__}: {exc} <- {type(root).__name__}: {root}"


def _raise_sdk(exc: BaseException) -> None:
    """把 openai SDK 异常翻译成统一异常；永远抛出，不返回。"""
    import openai

    status = getattr(exc, "status_code", None)
    if status:
        classify_status(int(status), str(exc))
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
        raise RetryableTransportError(_describe(exc)) from exc
    raise TransportStatusError(None, str(exc))


# ==================================================================
# Chat（对话 / 视觉 / 记忆摘要共用）
# ==================================================================
class ChatTransport(ABC):
    """一次补全或一条 token 流；重试/熔断由调用方（LlmClient）负责。"""

    name: str = "abstract"

    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout

    @abstractmethod
    async def complete(self, *, base_url: str, api_key: str, model: str,
                       messages: list, temperature: float, max_tokens: int,
                       extra_body: dict | None = None) -> str: ...

    @abstractmethod
    def stream(self, *, base_url: str, api_key: str, model: str,
               messages: list, temperature: float,
               extra_body: dict | None = None) -> AsyncIterator[str]: ...

    async def aclose(self) -> None: ...


class OpenAISDKChatTransport(ChatTransport):
    """官方 openai SDK：cover 全部 OpenAI 兼容端点（国内厂商主流接入方式）。"""

    name = "openai_sdk"

    def __init__(self, timeout: float = 60.0, client_factory=None):
        super().__init__(timeout)
        self._factory = client_factory      # 测试注入点；生产走 AsyncOpenAI
        self._clients: dict[tuple[str, str], object] = {}

    def _client(self, base_url: str, api_key: str):
        key = (base_url, api_key)
        client = self._clients.get(key)
        if client is None:
            base = _strip_suffix(base_url, "/chat/completions")
            if self._factory is not None:
                client = self._factory(base_url=base, api_key=api_key,
                                      timeout=self.timeout)
            else:
                from openai import AsyncOpenAI
                client = AsyncOpenAI(base_url=base, api_key=api_key or "EMPTY",
                                    timeout=self.timeout, max_retries=0)
            self._clients[key] = client
        return client

    async def complete(self, *, base_url: str, api_key: str, model: str,
                       messages: list, temperature: float, max_tokens: int,
                       extra_body: dict | None = None) -> str:
        client = self._client(base_url, api_key)
        try:
            resp = await client.chat.completions.create(
                model=model, messages=messages, temperature=temperature,
                max_tokens=max_tokens, extra_body=dict(extra_body) if extra_body else None)
        except Exception as e:
            _raise_sdk(e)
        content = resp.choices[0].message.content
        return content or ""

    async def stream(self, *, base_url: str, api_key: str, model: str,
                     messages: list, temperature: float,
                     extra_body: dict | None = None) -> AsyncIterator[str]:
        client = self._client(base_url, api_key)
        try:
            stream = await client.chat.completions.create(
                model=model, messages=messages, temperature=temperature, stream=True,
                extra_body=dict(extra_body) if extra_body else None)
        except Exception as e:
            _raise_sdk(e)
        try:
            async for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                text = getattr(delta, "content", None)
                if text:
                    yield text
        except Exception as e:
            _raise_sdk(e)

    async def aclose(self) -> None:
        for client in self._clients.values():
            closer = getattr(client, "close", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:      # 关闭异常不影响主流程
                    pass
        self._clients.clear()


class HttpChatTransport(ChatTransport):
    """httpx 直连（SDK 不可用或需要完全掌控报文时使用）。"""

    name = "http"

    def __init__(self, timeout: float = 60.0):
        super().__init__(timeout)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(self, *, base_url: str, api_key: str, model: str,
                       messages: list, temperature: float, max_tokens: int,
                       extra_body: dict | None = None) -> str:
        resp = await self._client.post(
            f"{_strip_suffix(base_url, '/chat/completions')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            json={"model": model, "messages": messages, "temperature": temperature,
                  "max_tokens": max_tokens, **(extra_body or {})})
        classify_status(resp.status_code, resp.text)
        return resp.json()["choices"][0]["message"]["content"] or ""

    async def stream(self, *, base_url: str, api_key: str, model: str,
                     messages: list, temperature: float,
                     extra_body: dict | None = None) -> AsyncIterator[str]:
        body = {"model": model, "messages": messages, "temperature": temperature,
                "stream": True, **(extra_body or {})}
        async with self._client.stream(
                "POST", f"{_strip_suffix(base_url, '/chat/completions')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                json=body) as resp:
            if resp.status_code >= 400:
                classify_status(resp.status_code, (await resp.aread()).decode("utf-8", "ignore"))
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                    delta = chunk["choices"][0].get("delta", {}).get("content")
                    if delta:
                        yield delta
                except (json.JSONDecodeError, KeyError, IndexError):
                    # 兼容网关偶发心跳/半包行，跳过而不中断流
                    continue


def select_chat_transport(base_url: str, timeout: float = 60.0) -> ChatTransport:
    """对话类模型：SDK 可用即走 SDK（兼容模式/智谱/DeepSeek/本地均适用）。"""
    if sdk_available():
        return OpenAISDKChatTransport(timeout=timeout)
    return HttpChatTransport(timeout=timeout)


# ==================================================================
# Speech（Whisper /audio/transcriptions 协议）
# ==================================================================
class SpeechTransport(ABC):
    name: str = "abstract"

    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout

    @abstractmethod
    async def transcribe(self, *, base_url: str, api_key: str, model: str,
                         audio: bytes, fmt: str) -> str: ...

    async def aclose(self) -> None: ...


class OpenAISDKSpeechTransport(SpeechTransport):
    name = "openai_sdk"

    def __init__(self, timeout: float = 60.0, client_factory=None):
        super().__init__(timeout)
        self._factory = client_factory
        self._client = None

    async def transcribe(self, *, base_url: str, api_key: str, model: str,
                         audio: bytes, fmt: str) -> str:
        if self._client is None:
            base = _strip_suffix(base_url, "/audio/transcriptions")
            if self._factory is not None:
                self._client = self._factory(base_url=base, api_key=api_key,
                                            timeout=self.timeout)
            else:
                from openai import AsyncOpenAI
                self._client = AsyncOpenAI(base_url=base, api_key=api_key or "EMPTY",
                                         timeout=self.timeout, max_retries=0)
        try:
            resp = await self._client.audio.transcriptions.create(
                file=(f"audio.{fmt}", audio, f"audio/{fmt}"),
                model=model, response_format="json")
        except Exception as e:
            _raise_sdk(e)
        return (getattr(resp, "text", "") or "").strip()

    async def aclose(self) -> None:
        closer = getattr(self._client, "close", None)
        if closer is not None:
            try:
                await closer()
            except Exception:
                pass
        self._client = None


class HttpSpeechTransport(SpeechTransport):
    name = "http"

    def __init__(self, timeout: float = 60.0):
        super().__init__(timeout)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0))

    async def transcribe(self, *, base_url: str, api_key: str, model: str,
                         audio: bytes, fmt: str) -> str:
        resp = await self._client.post(
            f"{_strip_suffix(base_url, '/audio/transcriptions')}/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            data={"model": model, "response_format": "json"},
            files={"file": (f"audio.{fmt}", audio, f"audio/{fmt}")})
        classify_status(resp.status_code, resp.text)
        return (resp.json().get("text") or "").strip()

    async def aclose(self) -> None:
        await self._client.aclose()


# 音频格式 → MIME（Qwen-ASR 用 data URI 传输，必须带正确的 mediatype）
_AUDIO_MIME = {"wav": "audio/wav", "mp3": "audio/mpeg", "m4a": "audio/mp4",
               "mp4": "audio/mp4", "webm": "audio/webm", "ogg": "audio/ogg",
               "opus": "audio/ogg", "flac": "audio/flac", "amr": "audio/amr",
               "aac": "audio/aac", "pcm": "audio/wav"}


def _audio_data_uri(audio: bytes, fmt: str) -> str:
    mime = _AUDIO_MIME.get((fmt or "wav").lower(), f"audio/{fmt or 'wav'}")
    return f"data:{mime};base64,{base64.b64encode(audio).decode('ascii')}"


class AliyunChatSpeechTransport(SpeechTransport):
    """阿里百炼 Qwen-ASR（qwen3-asr-flash）：走 OpenAI 兼容 /chat/completions。

    百炼的 qwen3-asr-flash 不在 /audio/transcriptions（该路径 404），而是把音频作为
    `input_audio` 内容块（支持 data URI base64）放进对话消息，文本从
    choices[0].message.content 返回。asr_options 是非标准参数，随报文一同下发。
    """

    name = "aliyun_chat"

    def __init__(self, timeout: float = 60.0):
        super().__init__(timeout)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0))

    async def transcribe(self, *, base_url: str, api_key: str, model: str,
                         audio: bytes, fmt: str) -> str:
        url = f"{_strip_suffix(base_url, '/chat/completions')}/chat/completions"
        body = {
            "model": model,
            "stream": False,
            "messages": [{"role": "user", "content": [
                {"type": "input_audio",
                 "input_audio": {"data": _audio_data_uri(audio, fmt)}}]}],
            "asr_options": {"enable_itn": True},
        }
        resp = await self._client.post(
            url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            json=body)
        classify_status(resp.status_code, resp.text)
        data = resp.json()
        choices = data.get("choices") or []
        return ((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""

    async def aclose(self) -> None:
        await self._client.aclose()


class DashScopeSpeechTransport(SpeechTransport):
    """DashScope 原生同步 ASR：/api/v1/services/aigc/multimodal-generation/generation。

    报文 input.messages[].content[].audio（data URI），
    结果在 output.choices[0].message.content[0].text。适用于后台直接填原生端点的情况。
    """

    name = "aliyun_native"

    def __init__(self, timeout: float = 60.0):
        super().__init__(timeout)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0))

    async def transcribe(self, *, base_url: str, api_key: str, model: str,
                         audio: bytes, fmt: str) -> str:
        base = (base_url or "").rstrip("/")
        if "multimodal-generation/generation" in base:
            url = base
        elif base.endswith("/api/v1"):
            url = f"{base}/services/aigc/multimodal-generation/generation"
        elif "/api/v1/services" in base:
            url = base
        else:
            url = f"{base}/api/v1/services/aigc/multimodal-generation/generation"
        body = {
            "model": model,
            "input": {"messages": [{"role": "user", "content": [
                {"audio": _audio_data_uri(audio, fmt)}]}]},
            "parameters": {"asr_options": {"enable_itn": True}},
        }
        resp = await self._client.post(
            url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            json=body)
        classify_status(resp.status_code, resp.text)
        choices = (resp.json().get("output") or {}).get("choices") or []
        if not choices:
            return ""
        blocks = (choices[0].get("message") or {}).get("content") or []
        return "".join(b.get("text") or "" for b in blocks).strip()

    async def aclose(self) -> None:
        await self._client.aclose()


def select_speech_transport(base_url: str, model: str = "",
                            timeout: float = 60.0) -> SpeechTransport:
    """语音转写传输选择。

    - 阿里 Qwen-ASR（模型名含 asr）+ compatible-mode 端点 → 对话协议（带音频内容块）；
    - 阿里 Qwen-ASR + 原生 /api/v1/services 端点 → DashScope 原生同步接口；
    - 其余（Whisper / 自建 FunASR / 本地 vLLM）→ OpenAI /audio/transcriptions。
    """
    base = base_url or ""
    is_qwen_asr = "asr" in (model or "").lower()
    # 原生 DashScope 端点（含裸 /api/v1 与完整 services 路径），排除 compatible-mode
    is_native = "/api/v1" in base and "/compatible-mode" not in base
    if is_qwen_asr and is_native:
        return DashScopeSpeechTransport(timeout=timeout)
    if is_qwen_asr and "aliyuncs.com" in base:
        return AliyunChatSpeechTransport(timeout=timeout)
    if is_native:
        return HttpSpeechTransport(timeout=timeout)
    if sdk_available():
        return OpenAISDKSpeechTransport(timeout=timeout)
    return HttpSpeechTransport(timeout=timeout)



# ==================================================================
# Image（文生图）
# ==================================================================
class ImageTransport(ABC):
    name: str = "abstract"

    @abstractmethod
    async def generate(self, *, base_url: str, api_key: str, model: str,
                       prompt: str, size: str, n: int,
                       stop_event: asyncio.Event | None = None) -> list[str]: ...

    async def aclose(self) -> None: ...


class OpenAISDKImageTransport(ImageTransport):
    """OpenAI 兼容文生图：`images.generate` 同步返回 URL 或 b64。"""

    name = "openai_sdk"

    def __init__(self, timeout: float = 60.0, client_factory=None):
        self.timeout = timeout
        self._factory = client_factory
        self._client = None

    async def generate(self, *, base_url: str, api_key: str, model: str,
                       prompt: str, size: str, n: int,
                       stop_event: asyncio.Event | None = None) -> list[str]:
        if self._client is None:
            base = _strip_suffix(base_url, "/images/generations")
            if self._factory is not None:
                self._client = self._factory(base_url=base, api_key=api_key,
                                            timeout=self.timeout)
            else:
                from openai import AsyncOpenAI
                self._client = AsyncOpenAI(base_url=base, api_key=api_key or "EMPTY",
                                         timeout=self.timeout, max_retries=0)
        try:
            resp = await self._client.images.generate(
                model=model, prompt=prompt, size=size, n=max(1, min(n, 4)))
        except Exception as e:
            _raise_sdk(e)
        urls: list[str] = []
        for item in getattr(resp, "data", None) or []:
            url = getattr(item, "url", None)
            if url:
                urls.append(url)
            elif getattr(item, "b64_json", None):
                urls.append("data:image/png;base64," + item.b64_json)
        if not urls:
            raise ImageGenerationError("文生图完成但未返回图片地址")
        return urls

    async def aclose(self) -> None:
        closer = getattr(self._client, "close", None)
        if closer is not None:
            try:
                await closer()
            except Exception:
                pass
        self._client = None


_DASHSCOPE_NATIVE = "https://dashscope.aliyuncs.com/api/v1"
# 阿里百炼的图像模型（万相/通义图像）只提供原生异步任务，兼容模式无 images 端点
_ALIYUN_IMAGE_PREFIXES = ("wan", "wanx", "qwen-image")


def image_native_base(base_url: str) -> str:
    """把模型配置里的地址折算成 DashScope 原生服务前缀（.../api/v1）。

    用户在后台通常填 OpenAI 兼容地址（.../compatible-mode/v1），但文生图在
    兼容模式下没有 /images/generations（实测 404），必须回到原生异步任务端点。
    专属 MaaS 实例（*.maas.aliyuncs.com）不承载图像异步任务，回落到公共域名。
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return _DASHSCOPE_NATIVE
    if "/api/v1" in base:
        return base.split("/api/v1", 1)[0] + "/api/v1"
    if "/compatible-mode/" in base:
        head = base.split("/compatible-mode/", 1)[0]
        if "maas.aliyuncs.com" in head:
            return _DASHSCOPE_NATIVE
        return head + "/api/v1"
    if "dashscope.aliyuncs.com" in base or "maas.aliyuncs.com" in base:
        return _DASHSCOPE_NATIVE
    return base


class DashScopeImageTransport(ImageTransport):
    """DashScope 原生异步任务：提交 task + 轮询（OpenAI 协议无法表达）。"""

    name = "dashscope_native"

    def __init__(self, poll_timeout_seconds: float = 120.0,
                 submit_timeout_seconds: float = 15.0):
        self._poll_timeout = poll_timeout_seconds
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(submit_timeout_seconds, connect=5.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generate(self, *, base_url: str, api_key: str, model: str,
                       prompt: str, size: str, n: int,
                       stop_event: asyncio.Event | None = None) -> list[str]:
        base = image_native_base(base_url)
        task_id = await self._submit(base, api_key, model, prompt, size, n)
        return await self._poll(base, api_key, task_id, stop_event)

    async def _submit(self, base_url: str, api_key: str, model: str,
                      prompt: str, size: str, n: int) -> str:
        try:
            resp = await self._client.post(
                f"{base_url.rstrip('/')}/services/aigc/text2image/image-synthesis",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json",
                         "X-DashScope-Async": "enable"},
                json={"model": model, "input": {"prompt": prompt},
                      "parameters": {"size": size, "n": max(1, min(n, 4))}})
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            raise ImageGenerationError("文生图服务连接失败，请稍后重试") from e
        if resp.status_code >= 400:
            raise ImageGenerationError(
                f"文生图提交失败: HTTP {resp.status_code} {resp.text[:200]}")
        task_id = (resp.json().get("output") or {}).get("task_id")
        if not task_id:
            raise ImageGenerationError("文生图提交异常：未返回任务 ID")
        return task_id

    async def _poll(self, base_url: str, api_key: str, task_id: str,
                    stop_event: asyncio.Event | None) -> list[str]:
        deadline = asyncio.get_running_loop().time() + self._poll_timeout
        url = f"{base_url.rstrip('/')}/tasks/{task_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
        while True:
            if stop_event is not None and stop_event.is_set():
                raise ImageCancelledError()
            await asyncio.sleep(2.0)
            try:
                resp = await self._client.get(url, headers=headers)
                data = resp.json() if resp.status_code == 200 else {}
            except (httpx.TimeoutException, httpx.ConnectError, ValueError) as e:
                data = {}
            status = (data.get("output") or {}).get("task_status", "")
            if status == "SUCCEEDED":
                urls = [r["url"] for r in (data["output"].get("results") or []) if r.get("url")]
                if not urls:
                    raise ImageGenerationError("文生图完成但未返回图片地址")
                return urls
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                message = (data["output"].get("message") or "生成失败")[:200]
                raise ImageGenerationError(f"文生图失败: {message}")
            if asyncio.get_running_loop().time() > deadline:
                raise ImageGenerationError("文生图超时，请稍后重试")


def select_image_transport(base_url: str, provider: str = "", model: str = "",
                           poll_timeout_seconds: float = 120.0,
                           submit_timeout_seconds: float = 15.0) -> ImageTransport:
    """文生图：阿里百炼图像模型走原生异步任务，其余 OpenAI 兼容端点走 SDK。

    判定顺序：provider=aliyun / 阿里域名 / 万相系列模型名（用户在后台可能把
    模型挂在 custom 提供方下）。阿里兼容模式的 /images/generations 并不存在，
    所以这里不看 base_url 是否含 compatible-mode。
    """
    base = base_url or ""
    is_aliyun = (provider in ("aliyun", "dashscope")
                 or "dashscope.aliyuncs.com" in base
                 or "maas.aliyuncs.com" in base
                 or (model or "").lower().startswith(_ALIYUN_IMAGE_PREFIXES))
    if is_aliyun:
        return DashScopeImageTransport(poll_timeout_seconds=poll_timeout_seconds,
                                        submit_timeout_seconds=submit_timeout_seconds)
    if sdk_available():
        return OpenAISDKImageTransport(timeout=submit_timeout_seconds)
    return DashScopeImageTransport(poll_timeout_seconds=poll_timeout_seconds,
                                    submit_timeout_seconds=submit_timeout_seconds)
