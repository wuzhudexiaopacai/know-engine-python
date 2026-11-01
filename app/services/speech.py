"""语音转写服务（STT，DashScope OpenAI 兼容协议的多模态音频输入）。

设计（沿用 llm_client 的弹性四件套）：
- tenacity 仅对超时/连接类瞬时故障重试；401/403/404（模型未开通等配置类错误）
  快速失败并携带可读信息，由 controller 转成 200 + success=false 的降级提示
- CircuitBreaker 熔断：上游持续故障时快速失败
- 无 Key 视为不可用（available=False），接口直接返回"未配置"提示而非 500
"""

from __future__ import annotations

import base64

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.infra.circuit_breaker import CircuitOpenError, get_breaker

logger = get_logger("app.speech")


class SpeechError(Exception):
    """转写失败（携带面向用户的可读信息）。"""


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))


class SpeechService:
    def __init__(self):
        cfg = get_settings().speech
        self.cfg = cfg
        self._base = cfg.base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.timeout_seconds, connect=10.0))
        self._breaker = get_breaker("speech", failure_threshold=3, recovery_seconds=60)

    @property
    def api_key(self) -> str:
        """独立 Key 优先，留空复用 LLM Key（同一 DashScope 账号）。"""
        return self.cfg.api_key or get_settings().llm.api_key

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def close(self) -> None:
        await self._client.aclose()

    async def _resolve(self) -> tuple[str, str, str, bool]:
        """解析当前生效的语音模型：(base_url, api_key, model_name, allow_empty)。

        后台「语音识别」用途绑定优先；未绑定回退环境变量 SPEECH__ 配置。
        """
        from app.modules.model import service as model_service
        try:
            rt = await model_service.resolve_role_runtime("speech")
        except Exception as e:
            logger.warning("speech_role_resolve_failed", error=str(e))
            rt = None
        if rt is not None:
            return rt.base_url, rt.api_key, rt.model_name, rt.allow_empty_key
        return (self._base, self.api_key, self.cfg.asr_model, False)

    async def transcribe(self, audio: bytes, fmt: str = "wav") -> str:
        """音频字节 → 文本。fmt 为音频格式（wav/mp3/pcm 等 DashScope 支持项）。"""
        base_url, api_key, model_name, allow_empty = await self._resolve()
        if not api_key and not allow_empty:
            raise SpeechError("语音识别未配置（请在后台上传「语音识别」用途的模型）")
        if len(audio) > self.cfg.max_audio_bytes:
            raise SpeechError("录音过长，请缩短后重试")
        data_url = f"data:;base64,{base64.b64encode(audio).decode()}"
        try:
            return await self._breaker.call(self._transcribe_with_retry, data_url, fmt,
                                            base_url, api_key, model_name, allow_empty)
        except SpeechError:
            raise
        except CircuitOpenError:
            raise SpeechError("语音识别服务暂时不可用，请稍后重试")
        except Exception as e:
            logger.warning("speech_transcribe_failed", error=str(e))
            raise SpeechError("语音识别失败，请稍后重试") from e

    async def _transcribe_with_retry(self, data_url: str, fmt: str, base_url: str,
                                     api_key: str, model_name: str, allow_empty: bool) -> str:

        @retry(
            retry=retry_if_exception(_is_retryable),
            wait=wait_random_exponential(multiplier=0.5, max=6),
            stop=stop_after_attempt(2),
            reraise=True,
        )
        async def _call() -> str:
            resp = await self._client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                json={"model": model_name, "stream": False, "messages": [
                    {"role": "user", "content": [
                        {"type": "input_audio",
                         "input_audio": {"data": data_url, "format": fmt}},
                    ]},
                ]},
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                raise SpeechError(f"转写上游抖动: HTTP {resp.status_code}")
            if resp.status_code >= 400:
                # 模型未开通/鉴权失败等配置类错误：带原始信息，不再重试
                raise SpeechError(
                    f"语音识别调用失败: HTTP {resp.status_code} {resp.text[:200]}")
            data = resp.json()
            content = data["choices"][0]["message"]["content"] or ""
            # 兼容部分模型把纯文本放在 reasoning/其它字段的情况
            return content.strip()

        return await _call()


def detect_audio_format(content_type: str, fallback: str = "wav") -> str:
    """从 multipart 上传的 content_type 推断 DashScope 音频格式，未知时兜底。"""
    return {"audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
            "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a",
            "audio/x-m4a": "m4a", "audio/webm": "webm", "audio/ogg": "ogg",
            "audio/flac": "flac", "audio/pcm": "pcm",
            "audio/amr": "amr"}.get(content_type.split(";")[0].strip(), fallback)


_service: SpeechService | None = None


def get_speech() -> SpeechService:
    global _service
    if _service is None:
        _service = SpeechService()
    return _service


async def close_speech() -> None:
    global _service
    if _service is not None:
        await _service.close()
        _service = None
