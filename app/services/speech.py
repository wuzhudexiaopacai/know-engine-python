"""语音转写服务（STT）。

传输实现由策略选择（app.services.model_transports.SpeechTransport）：
- 阿里百炼 qwen3-asr-flash → OpenAI 兼容 /chat/completions（音频以 input_audio 内容块传入），
  原生 /api/v1/services 端点 → DashScope 同步多模态生成接口；
- 其余（Whisper / 本地 FunASR / 自建兼容服务）→ /audio/transcriptions（SDK 或 httpx）。

设计（沿用 llm_client 的弹性四件套）：
- tenacity 仅对超时/连接/429/5xx 瞬时故障重试；401/403/404（模型未开通等
  配置类错误）快速失败并携带可读信息，由 controller 转成 200 + success=false
- CircuitBreaker 熔断：上游持续故障时快速失败
- 免鉴权端点（本地 FunASR）allow_empty_key，未配 Key 也视为可用
"""

from __future__ import annotations

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.infra.circuit_breaker import CircuitOpenError, get_breaker
from app.services.model_transports import (
    RetryableTransportError, TransportStatusError, select_speech_transport,
)

logger = get_logger("app.speech")


class SpeechError(Exception):
    """转写失败（携带面向用户的可读信息）。"""


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, RetryableTransportError)


class SpeechService:
    def __init__(self):
        cfg = get_settings().speech
        self.cfg = cfg
        self._base = cfg.base_url.rstrip("/")
        self._breaker = get_breaker("speech", failure_threshold=3, recovery_seconds=60)

    @property
    def api_key(self) -> str:
        """独立 Key 优先，留空复用 LLM Key（同一 DashScope 账号）。"""
        return self.cfg.api_key or get_settings().llm.api_key

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def close(self) -> None:
        """传输客户端按请求创建并关闭，此处无需释放资源。"""

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
        """音频字节 → 文本。fmt 为音频格式（wav/mp3/pcm 等）。"""
        base_url, api_key, model_name, allow_empty = await self._resolve()
        if not api_key and not allow_empty:
            raise SpeechError("语音识别未配置（请在后台上传「语音识别」用途的模型）")
        if len(audio) > self.cfg.max_audio_bytes:
            raise SpeechError("录音过长，请缩短后重试")
        try:
            return await self._breaker.call(self._transcribe_with_retry, audio, fmt,
                                            base_url, api_key, model_name)
        except SpeechError:
            raise
        except CircuitOpenError:
            raise SpeechError("语音识别服务暂时不可用，请稍后重试")
        except TransportStatusError as e:
            raise SpeechError(f"语音识别调用失败: {e}") from e
        except Exception as e:
            logger.warning("speech_transcribe_failed", error=str(e))
            raise SpeechError("语音识别失败，请稍后重试") from e

    async def _transcribe_with_retry(self, audio: bytes, fmt: str,
                                     base_url: str, api_key: str,
                                     model_name: str) -> str:
        transport = select_speech_transport(base_url, model_name,
                                            timeout=self.cfg.timeout_seconds)

        @retry(
            retry=retry_if_exception(_is_retryable),
            wait=wait_random_exponential(multiplier=0.5, max=6),
            stop=stop_after_attempt(2),
            reraise=True,
        )
        async def _call() -> str:
            return await transport.transcribe(
                base_url=base_url, api_key=api_key, model=model_name,
                audio=audio, fmt=fmt)

        try:
            return await _call()
        finally:
            await transport.aclose()


def detect_audio_format(content_type: str, fallback: str = "wav") -> str:
    """从 multipart 上传的 content_type 推断音频格式，未知时兜底。"""
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
