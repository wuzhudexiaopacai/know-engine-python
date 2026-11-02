"""LLM 客户端（对话/视觉/记忆共用）。

传输实现由策略模式选择（app.services.model_transports.ChatTransport）：
- 默认官方 openai SDK：覆盖阿里百炼 compatible-mode、智谱、DeepSeek、Kimi
  以及本地 vLLM/Ollama 等全部 OpenAI 兼容端点；
- SDK 不可用时自动回落 httpx 直连，行为完全一致。

弹性设计（对应"降级重试"工程要求）：
1. tenacity 指数退避 + 抖动重试：仅对可重试异常（超时/连接/429/5xx）重试，
   401/403 等配置类错误立即失败，防止无意义重试放大故障
2. CircuitBreaker 熔断：上游持续故障时快速失败，由上层走降级路径
3. fallback：无 API Key / 熔断打开时 available=False，管道各环节自行降级
   （跳过改写、默认路由、通用对话直答），而不是整条链路报错
4. 流式：token 级产出；建连阶段可重试，流出中途断裂时保留已产出部分
"""

from __future__ import annotations

import asyncio
import random
from typing import AsyncIterator

from tenacity import (
    retry, retry_if_exception, stop_after_attempt, wait_random_exponential,
)

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.infra.circuit_breaker import CircuitOpenError, get_breaker
from app.infra.json_fix import fix_json
from app.services.model_transports import (
    ChatTransport, ModelCapabilityError, RetryableTransportError,
    select_chat_transport,
)

logger = get_logger("app.llm")


def _is_retryable(exc: BaseException) -> bool:
    """仅瞬时故障可重试：传输层已把超时/连接/429/5xx统一映射为 Retryable。"""
    return isinstance(exc, RetryableTransportError)


class LlmClient:
    def __init__(self, *, base_url: str | None = None, api_key: str | None = None,
                 chat_model: str | None = None, allow_empty_key: bool = False,
                 supports_vision: bool = False):
        """默认从环境变量配置构造；传入覆盖项则指向管理员配置的某个模型。

        allow_empty_key 用于本地部署（Ollama/vLLM）等无需鉴权的端点。
        supports_vision 标记该对话模型是否支持图片输入（多模态），
        决定 RAG 生成时是否把文档图片作为 image_url 传入。
        """
        cfg = get_settings().llm
        self.cfg = cfg
        self._base = (base_url or cfg.base_url).rstrip("/")
        self._api_key = api_key if api_key is not None else cfg.api_key
        self._allow_empty_key = allow_empty_key
        self.supports_vision = supports_vision
        self.chat_model = chat_model or cfg.chat_model
        self._transport: ChatTransport = select_chat_transport(
            self._base, timeout=cfg.timeout_seconds)
        self._breaker = get_breaker(f"llm:{self._base}:{self.chat_model}",
                                    failure_threshold=5, recovery_seconds=30)

    # ---------------- 可用性 ----------------
    @property
    def available(self) -> bool:
        """无 Key 且未显式允许匿名端点时视为不可用（管道将走降级路径）。"""
        return bool(self._api_key) or self._allow_empty_key

    def _thinking_off(self) -> dict:
        """本地推理模型（如 qwen3）默认先产出一大段思维链，关闭后首 token可从分钟级降到秒级。"""
        return {"reasoning_effort": "none"} if self._allow_empty_key else {}

    def _completion_extra(self) -> dict:
        """非标准参数的附加字段：只发给能识别它们的端点，避免 OpenAI 官方 400。

        enable_thinking 是阿里系扩展字段（compatible-mode / MaaS），
        发给 api.openai.com 会被当作未知参数拒绝。
        """
        body = self._thinking_off()
        if "aliyuncs.com" in self._base:
            body["enable_thinking"] = False
        return body

    @property
    def signature(self) -> str:
        """端点+模型的唯一标识（用于进程内能力标记，如"纯文本模型"）。"""
        return f"{self._base}:{self.chat_model}"

    @property
    def flash_model(self) -> str:
        """低延迟辅助任务用的模型：选中动态模型时复用同一模型（避免跨供应商错配）。"""
        return self.chat_model

    async def close(self) -> None:
        await self._transport.aclose()

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
        extra_body = self._completion_extra()

        @retry(
            retry=retry_if_exception(_is_retryable),
            wait=wait_random_exponential(multiplier=0.5, max=8),
            stop=stop_after_attempt(self.cfg.max_retries + 1),
            reraise=True,
        )
        async def _call() -> str:
            return await self._transport.complete(
                base_url=self._base, api_key=self._api_key, model=model,
                messages=messages, temperature=temperature, max_tokens=max_tokens,
                extra_body=extra_body)

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
        extra_body = self._thinking_off()

        started = False
        last_error: Exception | None = None
        attempts = self.cfg.max_retries + 1
        for attempt in range(attempts):
            try:
                async for token in self._transport.stream(
                        base_url=self._base, api_key=self._api_key,
                        model=self.chat_model, messages=messages,
                        temperature=temperature, extra_body=extra_body):
                    started = True
                    yield token
                await self._breaker._on_success()
                return
            except RetryableTransportError as e:
                if started:
                    # 流出中途断裂：已推送内容仍然有效，按部分答案正常收尾
                    logger.warning("llm_stream_broken", error=str(e))
                    return
                last_error = e
                logger.warning("llm_stream_retry", attempt=attempt + 1, error=str(e),
                               endpoint=self._base, model=self.chat_model)
                if attempt + 1 < attempts:
                    # 指数退避 + 抖动：瞬时网络抖动（DNS/连接重置）需要时间恢复，
                    # 连续同秒重试只会把一次抖动放大成一轮对话失败
                    await asyncio.sleep(min(0.5 * 2 ** attempt, 4.0)
                                        + random.uniform(0, 0.3))
                continue
            except Exception as e:
                if started:
                    logger.warning("llm_stream_broken", error=str(e))
                    return
                await self._breaker._on_failure()
                raise
        # 建连多次失败：降级为非流式补全。SSE 链路异常但普通请求仍可用时，
        # 用户依然能拿到完整答案（整段作为一次 token 下发）
        logger.warning("llm_stream_fallback_complete", endpoint=self._base,
                       model=self.chat_model, error=str(last_error))
        try:
            text = await self._chat_with_retry(messages, self.chat_model, temperature, 2000)
        except Exception as e:
            await self._breaker._on_failure()
            raise CircuitOpenError(f"LLM 流式建连失败: {last_error}") from e
        if text:
            yield text
        await self._breaker._on_success()


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
                  allow_empty_key: bool = False,
                  supports_vision: bool = False) -> LlmClient:
    """按管理员配置的模型构造客户端（同一模型复用连接池，配置变更后自动换新）。"""
    cached = _dynamic_clients.get(model_id)
    if cached is not None and cached[0] == lock_version:
        return cached[1]
    client = LlmClient(base_url=base_url, api_key=api_key, chat_model=chat_model,
                       allow_empty_key=allow_empty_key, supports_vision=supports_vision)
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
