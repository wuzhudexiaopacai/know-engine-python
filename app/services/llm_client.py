"""LLM 客户端（OpenAI 兼容协议，默认 DashScope）。

弹性设计（对应"降级重试"工程要求）：
1. tenacity 指数退避 + 抖动重试：仅对可重试异常（超时/连接/429/5xx）重试，
   401/403 等配置类错误立即失败，防止无意义重试放大故障
2. CircuitBreaker 熔断：DashScope 持续故障时快速失败，由上层走降级路径
3. fallback：无 API Key / 熔断打开时 available=False，管道各环节自行降级
   （跳过改写、默认路由、通用对话直答），而不是整条链路报错
4. 流式：SSE 增量解析，token 级产出；生成中途中断时返回已产出部分
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx
from tenacity import (
    retry, retry_if_exception, stop_after_attempt, wait_random_exponential,
)

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.infra.circuit_breaker import CircuitOpenError, get_breaker
from app.infra.json_fix import fix_json

logger = get_logger("app.llm")


class ModelCapabilityError(Exception):
    """模型能力不匹配（如把纯文本模型当视觉模型用）：配置问题，不是依赖故障。

    单独定义的意义：上层可据此立即放弃剩余任务并给出明确提示，
    熔断器也不会把它计入失败窗口（否则 50 张图会被误判成依赖不可用）。
    """


class _Retryable(Exception):
    """内部标记异常：值得重试的瞬时故障。"""


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (_Retryable, httpx.TimeoutException, httpx.ConnectError))


class LlmClient:
    def __init__(self, *, base_url: str | None = None, api_key: str | None = None,
                 chat_model: str | None = None, allow_empty_key: bool = False):
        """默认从环境变量配置构造；传入覆盖项则指向管理员配置的某个模型。

        allow_empty_key 用于本地部署（Ollama/vLLM）等无需鉴权的端点。
        """
        cfg = get_settings().llm
        self.cfg = cfg
        self._base = (base_url or cfg.base_url).rstrip("/")
        self._api_key = api_key if api_key is not None else cfg.api_key
        self._allow_empty_key = allow_empty_key
        self.chat_model = chat_model or cfg.chat_model
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.timeout_seconds, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._breaker = get_breaker(f"llm:{self._base}:{self.chat_model}",
                                    failure_threshold=5, recovery_seconds=30)

    # ---------------- 可用性 ----------------
    @property
    def available(self) -> bool:
        """无 Key 且未显式允许匿名端点时视为不可用（管道将走降级路径）。"""
        return bool(self._api_key) or self._allow_empty_key

    def _headers(self) -> dict[str, str]:
        """有密钥才注入 Bearer；本地模型通常不校验，避免空 Authorization 头。"""
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _thinking_off(self) -> dict:
        """本地推理模型（如 qwen3）默认先产出一大段思维链，关闭后首 token 可从分钟级降到秒级。"""
        return {"reasoning_effort": "none"} if self._allow_empty_key else {}

    @property
    def signature(self) -> str:
        """端点+模型的唯一标识（用于进程内能力标记，如"纯文本模型"）。"""
        return f"{self._base}:{self.chat_model}"

    @property
    def flash_model(self) -> str:
        """低延迟辅助任务用的模型：选中动态模型时复用同一模型（避免跨供应商错配）。"""
        return self.chat_model

    async def close(self) -> None:
        await self._client.aclose()

    # ---------------- 非流式 ----------------
    async def chat(self, prompt: str, *, system: str | None = None,
                   model: str | None = None, temperature: float = 0.3,
                   max_tokens: int = 2000) -> str:
        """单轮补全。model=None 用主模型；辅助任务传 cfg.flash_model。"""
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        return await self.chat_messages(messages, model=model,
                                        temperature=temperature, max_tokens=max_tokens)

    async def chat_messages(self, messages: list, *, model: str | None = None,
                            temperature: float = 0.3, max_tokens: int = 2000) -> str:
        """通用消息补全：多模态（文本 + image_url）与自定义结构共用同一重试/熔断链路。"""
        if not self.available:
            raise CircuitOpenError("LLM 未配置 API Key")
        return await self._breaker.call(
            self._chat_with_retry, messages, model or self.chat_model, temperature, max_tokens,
            skip_failure_on=(ModelCapabilityError,))

    async def chat_json(self, prompt: str, *, system: str | None = None,
                       model: str | None = None) -> dict | list | None:
        """要求 JSON 输出的补全：经 7 步修复管道解析，失败返回 None。"""
        raw = await self.chat(prompt + "\n\n只输出合法 JSON，不要任何额外文本。",
                              system=system, model=model)
        return json.loads(fix_json(raw)) if raw else None

    async def _chat_with_retry(self, messages: list, model: str,
                               temperature: float, max_tokens: int) -> str:

        @retry(
            retry=retry_if_exception(_is_retryable),
            wait=wait_random_exponential(multiplier=0.5, max=8),
            stop=stop_after_attempt(self.cfg.max_retries + 1),
            reraise=True,
        )
        async def _call() -> str:
            resp = await self._client.post(
                f"{self._base}/chat/completions",
                headers=self._headers(),
                json={"model": model, "messages": messages,
                      "temperature": temperature, "max_tokens": max_tokens,
                      # 辅助小任务关闭 CoT 思考模式，降低延迟
                      "enable_thinking": False, **self._thinking_off()},
            )
            self._raise_for_status(resp)
            data = resp.json()
            return data["choices"][0]["message"]["content"] or ""

        return await _call()

    # ---------------- 流式 ----------------
    async def stream_chat(self, prompt: str, *, system: str | None = None,
                          temperature: float = 0.5,
                          messages: list | None = None) -> AsyncIterator[str]:
        """Token 级异步生成器。

        messages 非空时直接透传（多模态文本 + image_url）；否则按 prompt/system 组装。
        建连阶段受熔断与重试保护；流出开始后若中断，
        将已产出部分视为部分答案正常收尾（不让整个对话失败）。
        """
        if not self.available:
            raise CircuitOpenError("LLM 未配置 API Key")
        if messages is None:
            messages = ([{"role": "system", "content": system}] if system else []) + \
                       [{"role": "user", "content": prompt}]

        stream_ctx = await self._open_stream_with_retry(messages, temperature)
        try:
            async for token in self._parse_sse_stream(stream_ctx["resp"]):
                yield token
            await self._breaker._on_success()
        except httpx.HTTPError as e:
            # 流出中途断裂：记录后正常结束，已推送内容仍然有效
            logger.warning("llm_stream_broken", error=str(e))
        finally:
            # 显式关闭 stream 上下文（cm 已被 __aenter__ 消费，只能调 __aexit__）
            try:
                await stream_ctx["cm"].__aexit__(None, None, None)
            except Exception:  # pragma: no cover - 关闭异常无需上抛
                pass

    async def _open_stream_with_retry(self, messages: list, temperature: float) -> dict:
        """流式建连（带重试）：成功返回已打开的 stream 上下文。"""
        last_error: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                cm = self._client.stream(
                    "POST", f"{self._base}/chat/completions",
                    headers=self._headers(),
                    json={"model": self.chat_model, "messages": messages,
                          "temperature": temperature, "stream": True,
                          **self._thinking_off()},
                )
                resp = await cm.__aenter__()
                if resp.status_code >= 500 or resp.status_code == 429:
                    await cm.__aexit__(None, None, None)
                    raise _Retryable(f"LLM 流式建连失败: HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    await cm.__aexit__(None, None, None)
                    resp.raise_for_status()
                return {"resp": resp, "cm": cm}
            except (httpx.TimeoutException, httpx.ConnectError, _Retryable) as e:
                last_error = e
                logger.warning("llm_stream_retry", attempt=attempt + 1, error=str(e))
                continue
        await self._breaker._on_failure()
        raise CircuitOpenError(f"LLM 流式建连失败: {last_error}")

    @staticmethod
    async def _parse_sse_stream(resp: httpx.Response) -> AsyncIterator[str]:
        """OpenAI SSE 协议解析：data: {...} / data: [DONE]。"""
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
                # 兼容网关偶发的心跳/半包行，跳过而不中断流
                continue

    # ---------------- 状态码分类 ----------------
    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code == 200:
            return
        if resp.status_code in (429, 500, 502, 503, 504):
            raise _Retryable(f"LLM 依赖抖动: HTTP {resp.status_code}")
        body = resp.text[:300]
        low = body.lower()
        # 400/422 中的「多模态不支持」是选错模型，重试与熔断都无意义
        if resp.status_code in (400, 422) and (
                "multimodal" in low or "image input" in low
                or "does not support image" in low or "unsupported image" in low):
            raise ModelCapabilityError("模型不支持图片输入")
        # 404 模型不存在（如把 embedding 模型名填进视觉配置）：配置错误，同样不计熔断
        if resp.status_code == 404 and ("not found" in low or "not_found" in low):
            raise ModelCapabilityError("模型不存在或名称错误")
        # 401/403/404/400：配置或请求问题，重试无意义
        raise httpx.HTTPStatusError(f"LLM 调用失败: HTTP {resp.status_code} {body[:200]}",
                                    request=resp.request, response=resp)


_client: LlmClient | None = None
# 管理员配置的动态模型客户端：按 (id, lock_version) 缓存，配置更新后 lock_version 变化自动重建
_dynamic_clients: dict[int, tuple[int, LlmClient]] = {}


def get_llm() -> LlmClient:
    """环境变量配置的默认客户端（辅助任务/未配置模型时的兜底）。"""
    global _client
    if _client is None:
        _client = LlmClient()
    return _client


def get_model_llm(model_id: int, lock_version: int, base_url: str,
                  api_key: str, chat_model: str,
                  allow_empty_key: bool = False) -> LlmClient:
    """按管理员配置的模型构造客户端（同一模型复用连接池，配置变更后自动换新）。"""
    cached = _dynamic_clients.get(model_id)
    if cached is not None and cached[0] == lock_version:
        return cached[1]
    client = LlmClient(base_url=base_url, api_key=api_key, chat_model=chat_model,
                       allow_empty_key=allow_empty_key)
    _dynamic_clients[model_id] = (lock_version, client)
    return client


async def close_llm() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
    for _, (_, client) in list(_dynamic_clients.items()):
        await client.close()
    _dynamic_clients.clear()
