"""模型传输层策略测试（不访问网络）。

覆盖：
- 协议/端点选择（对话走 SDK、原生语音/文生图走 httpx、SDK 缺失回落）
- openai SDK 传输的请求参数与流式增量解析
- 状态码归类（可重试 / 能力不匹配 / 直接失败）
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.services import model_transports as mt

COMPAT = "https://llm-x.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
NATIVE = "https://dashscope.aliyuncs.com/api/v1"


class TestSelection:
    def test_chat_prefers_sdk(self, monkeypatch):
        monkeypatch.setattr(mt, "sdk_available", lambda: True)
        assert mt.select_chat_transport(COMPAT).name == "openai_sdk"

    def test_chat_falls_back_to_http(self, monkeypatch):
        monkeypatch.setattr(mt, "sdk_available", lambda: False)
        assert mt.select_chat_transport(COMPAT).name == "http"

    def test_speech_native_uses_http(self, monkeypatch):
        monkeypatch.setattr(mt, "sdk_available", lambda: True)
        t = mt.select_speech_transport(f"{NATIVE}/services/audio/asr/transcription")
        assert t.name == "http"

    def test_speech_compatible_uses_sdk(self, monkeypatch):
        monkeypatch.setattr(mt, "sdk_available", lambda: True)
        assert mt.select_speech_transport(COMPAT).name == "openai_sdk"

    def test_qwen_asr_compatible_uses_chat_protocol(self, monkeypatch):
        monkeypatch.setattr(mt, "sdk_available", lambda: True)
        assert mt.select_speech_transport(COMPAT, "qwen3-asr-flash").name == "aliyun_chat"

    def test_qwen_asr_native_uses_dashscope(self):
        assert mt.select_speech_transport(NATIVE, "qwen3-asr-flash").name == "aliyun_native"

    def test_local_whisper_keeps_whisper_protocol(self, monkeypatch):
        monkeypatch.setattr(mt, "sdk_available", lambda: True)
        assert mt.select_speech_transport(
            "http://127.0.0.1:8001/v1", "sensevoice").name == "openai_sdk"

    def test_image_native_uses_dashscope(self, monkeypatch):
        monkeypatch.setattr(mt, "sdk_available", lambda: True)
        assert mt.select_image_transport(NATIVE).name == "dashscope_native"

    def test_image_openai_uses_sdk(self, monkeypatch):
        monkeypatch.setattr(mt, "sdk_available", lambda: True)
        assert mt.select_image_transport("https://api.openai.com/v1").name == "openai_sdk"

    def test_aliyun_image_uses_native_even_on_compatible_mode(self):
        """阿里兼容模式没有 /images/generations（实测 404），必须走原生异步任务。"""
        assert mt.select_image_transport(
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            provider="aliyun", model="wan2.2-t2i-flash").name == "dashscope_native"

    def test_wan_model_name_routes_to_native(self, monkeypatch):
        """模型挂在 custom 提供方下也要按模型名识别阿里图像模型。"""
        monkeypatch.setattr(mt, "sdk_available", lambda: True)
        assert mt.select_image_transport(
            "https://example.com/v1", model="wanx2.1-t2i-turbo").name == "dashscope_native"


class TestImageBaseNormalization:
    def test_compatible_mode_to_native_prefix(self):
        assert mt.image_native_base(
            "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ) == "https://dashscope.aliyuncs.com/api/v1"

    def test_maas_host_falls_back_to_public_dashscope(self):
        """专属 MaaS 实例不承载图像异步任务，回落到公共 DashScope 域名。"""
        assert mt.image_native_base(COMPAT) == NATIVE

    def test_native_service_path_keeps_prefix(self):
        assert mt.image_native_base(
            f"{NATIVE}/services/aigc/text2image/image-synthesis"
        ) == NATIVE


# ------------------------------------------------------------------
# SDK 传输：注入假客户端，验证报文与解析
# ------------------------------------------------------------------
class _FakeCompletions:
    def __init__(self, result):
        self._result = result
        self.kwargs: dict | None = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self._result


class _FakeChatClient:
    def __init__(self, result):
        self.chat = SimpleNamespace(completions=_FakeCompletions(result))


async def _aiter(items):
    for item in items:
        yield item


class TestSDKChatTransport:
    def test_complete_sends_extra_body(self):
        fake = _FakeChatClient(SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))]))
        transport = mt.OpenAISDKChatTransport(client_factory=lambda **_: fake)
        out = asyncio.run(transport.complete(
            base_url=COMPAT, api_key="sk-test", model="qwen3",
            messages=[{"role": "user", "content": "ping"}],
            temperature=0.0, max_tokens=8, extra_body={"enable_thinking": False}))
        assert out == "pong"
        kwargs = fake.chat.completions.kwargs
        assert kwargs["model"] == "qwen3"
        assert kwargs["max_tokens"] == 8
        assert kwargs["extra_body"] == {"enable_thinking": False}
        assert asyncio.run(transport.aclose()) is None

    def test_complete_with_tools_parses_tool_calls(self):
        call = SimpleNamespace(id="c1",
                               function=SimpleNamespace(name="s__search",
                                                        arguments='{"q":"x"}'))
        message = SimpleNamespace(content=None, tool_calls=[call])
        fake = _FakeChatClient(SimpleNamespace(
            choices=[SimpleNamespace(message=message)]))
        transport = mt.OpenAISDKChatTransport(client_factory=lambda **_: fake)
        text, calls = asyncio.run(transport.complete_with_tools(
            base_url=COMPAT, api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.3, max_tokens=100,
            tools=[{"type": "function", "function": {"name": "s__search"}}]))
        assert text == ""
        assert calls == [{"id": "c1", "name": "s__search", "arguments": '{"q":"x"}'}]

    def test_stream_yields_delta_and_skips_empty_choices(self):
        chunks = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="你"))]),
            SimpleNamespace(choices=[]),                       # 网关心跳/空块
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="好"))]),
        ]
        transport = mt.OpenAISDKChatTransport(
            client_factory=lambda **_: _FakeChatClient(_aiter(chunks)))

        async def collect() -> list[str]:
            return [t async for t in transport.stream(
                base_url=COMPAT, api_key="k", model="m",
                messages=[{"role": "user", "content": "hi"}], temperature=0.5)]

        assert asyncio.run(collect()) == ["你", "好"]


class TestStatusClassification:
    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    def test_retryable(self, status):
        with pytest.raises(mt.RetryableTransportError):
            mt.classify_status(status, "busy")

    def test_multimodal_is_capability_error(self):
        with pytest.raises(mt.ModelCapabilityError, match="不支持图片"):
            mt.classify_status(400, "This model does not support image input")

    def test_model_not_found_is_capability_error(self):
        with pytest.raises(mt.ModelCapabilityError, match="模型不存在"):
            mt.classify_status(404, '{"error":"model not found"}')

    def test_auth_error_is_status_error(self):
        with pytest.raises(mt.TransportStatusError) as info:
            mt.classify_status(401, "invalid api key")
        assert info.value.status_code == 401
        assert "invalid api key" in info.value.detail

    def test_success_returns_none(self):
        assert mt.classify_status(200, "ok") is None


class TestStreamFallback:
    """SSE 建连失败时降级为非流式补全：用户仍能拿到完整答案。"""

    def test_stream_falls_back_to_complete(self, monkeypatch):
        from app.services import llm_client as lc

        client = lc.LlmClient(base_url="https://example.com/v1",
                              api_key="sk-test", chat_model="m")

        class _FlakyTransport:
            name = "fake"

            async def complete(self, **_):
                return "降级答案"

            def stream(self, **_):
                raise mt.RetryableTransportError("connection reset by peer")

            async def aclose(self):
                pass

        client._transport = _FlakyTransport()
        client.cfg = SimpleNamespace(max_retries=1)

        async def _noop_sleep(*_, **__):
            return None

        monkeypatch.setattr(lc.asyncio, "sleep", _noop_sleep)

        async def collect():
            return [t async for t in client.stream_chat("hi")]

        assert asyncio.run(collect()) == ["降级答案"]


class TestLocalHostRewrite:
    """容器内 localhost 指向容器自身：本地模型地址需改写为宿主机别名。"""

    def test_loopback_rewritten_in_container(self, monkeypatch):
        from app.modules.model import service as ms

        monkeypatch.setattr(ms, "_host_alias", lambda: "host.docker.internal")
        assert (ms._rewrite_local_url("http://localhost:11434/v1")
                == "http://host.docker.internal:11434/v1")
        assert (ms._rewrite_local_url("http://127.0.0.1:8080")
                == "http://host.docker.internal:8080")
        assert (ms._rewrite_local_url("http://[::1]:8001")
                == "http://host.docker.internal:8001")

    def test_cloud_and_service_hosts_untouched(self, monkeypatch):
        from app.modules.model import service as ms

        monkeypatch.setattr(ms, "_host_alias", lambda: "host.docker.internal")
        cloud = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert ms._rewrite_local_url(cloud) == cloud
        assert ms._rewrite_local_url("http://ollama:11434/v1") == "http://ollama:11434/v1"

    def test_local_dev_keeps_loopback_reachable(self, monkeypatch):
        from app.modules.model import service as ms

        monkeypatch.setattr(ms, "_host_alias", lambda: "localhost")
        assert (ms._rewrite_local_url("http://127.0.0.1:11434/v1")
                == "http://localhost:11434/v1")


class _FakeHttpResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeHttpClient:
    def __init__(self, payload):
        self._payload = payload
        self.url: str | None = None
        self.kwargs: dict | None = None

    async def post(self, url, **kwargs):
        self.url, self.kwargs = url, kwargs
        return _FakeHttpResponse(self._payload)

    async def aclose(self):
        return None


class TestAliyunSpeechTransport:
    """阿里 Qwen-ASR 两种协议：对话内容块 / 原生多模态生成。"""

    def test_chat_protocol_sends_input_audio_data_uri(self):
        fake = _FakeHttpClient({"choices": [{"message": {"content": " 你好 "}}]})
        transport = mt.AliyunChatSpeechTransport()
        transport._client = fake
        out = asyncio.run(transport.transcribe(
            base_url=COMPAT, api_key="sk-test", model="qwen3-asr-flash",
            audio=b"\x00\x01", fmt="wav"))
        assert out == "你好"
        assert fake.url == f"{COMPAT}/chat/completions"
        body = fake.kwargs["json"]
        audio = body["messages"][0]["content"][0]["input_audio"]["data"]
        assert audio.startswith("data:audio/wav;base64,")
        assert body["asr_options"]["enable_itn"] is True
        assert asyncio.run(transport.aclose()) is None

    def test_native_protocol_parses_content_blocks(self):
        fake = _FakeHttpClient({"output": {"choices": [
            {"message": {"content": [{"text": "你好"}, {"text": "世界"}]}}]}})
        transport = mt.DashScopeSpeechTransport()
        transport._client = fake
        out = asyncio.run(transport.transcribe(
            base_url=NATIVE, api_key="sk-test", model="qwen3-asr-flash",
            audio=b"\x00", fmt="mp3"))
        assert out == "你好世界"
        assert fake.url.endswith("/services/aigc/multimodal-generation/generation")
        body = fake.kwargs["json"]
        assert body["input"]["messages"][0]["content"][0]["audio"].startswith(
            "data:audio/mpeg;base64,")

