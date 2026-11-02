"""向量化链路单元测试。

不访问真实网络/数据库：
- 协议选择（阿里原生 vs OpenAI 兼容）、端点补全、报文构造、响应保序解析
- 本地降级向量（维度一致性、确定性、L2 归一化）
- _embed_batch 端到端：httpx.MockTransport 拦截请求，验证报文格式与解析结果
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from app.services import model_protocols as mp
from app.services.embedding import EmbeddingService

# 典型端点
DASHSCOPE_NATIVE = ("https://dashscope.aliyuncs.com"
                    "/api/v1/services/embeddings/text-embedding/text-embedding")
MAAS_NATIVE = ("https://ws-xxx.cn-beijing.maas.aliyuncs.com"
               "/api/v1/services/embeddings/text-embedding/text-embedding")
COMPAT = "https://dashscope.aliyuncs.com/compatible-mode/v1"
LOCAL = "http://127.0.0.1:8080/v1"


class TestSelectEmbeddingProtocol:
    """协议选择：按 URL 路径特征判定（MaaS 专属域名与公有云路径一致）。"""

    @pytest.mark.parametrize("url", [DASHSCOPE_NATIVE, MAAS_NATIVE])
    def test_native_urls_select_aliyun_native(self, url):
        assert mp.select_embedding_protocol(url).name == "aliyun_native"

    @pytest.mark.parametrize("url", [COMPAT, LOCAL, "https://api.openai.com/v1"])
    def test_other_urls_select_openai_compat(self, url):
        assert mp.select_embedding_protocol(url).name == "openai"


class TestEndpoint:
    def test_openai_appends_embeddings(self):
        p = mp.OpenAICompatEmbedding()
        assert p.endpoint(COMPAT) == f"{COMPAT}/embeddings"

    def test_openai_keeps_full_path(self):
        p = mp.OpenAICompatEmbedding()
        assert p.endpoint(f"{LOCAL}/embeddings") == f"{LOCAL}/embeddings"

    def test_native_keeps_full_path(self):
        assert mp.AliyunNativeEmbedding().endpoint(MAAS_NATIVE) == MAAS_NATIVE

    def test_native_completes_api_v1_base(self):
        p = mp.AliyunNativeEmbedding()
        assert p.endpoint("https://dashscope.aliyuncs.com/api/v1") == DASHSCOPE_NATIVE


class TestBuildPayload:
    def test_openai_payload(self):
        body = mp.OpenAICompatEmbedding().build_payload("m", ["甲", "乙"], None, "custom")
        assert body == {"model": "m", "input": ["甲", "乙"], "encoding_format": "float"}

    def test_openai_dimensions_only_for_known_providers(self):
        """dimensions 参数仅传给确认支持的供应商，避免本地/自定义端点 400。"""
        p = mp.OpenAICompatEmbedding()
        assert p.build_payload("m", ["t"], 1024, "aliyun")["dimensions"] == 1024
        assert "dimensions" not in p.build_payload("m", ["t"], 1024, "custom")
        assert "dimensions" not in p.build_payload("m", ["t"], None, "aliyun")

    def test_aliyun_native_payload(self):
        body = mp.AliyunNativeEmbedding().build_payload("m", ["甲", "乙"], 1024, "aliyun")
        assert body == {
            "model": "m",
            "input": {"texts": ["甲", "乙"]},
            "parameters": {"text_type": "document", "output_dimension": 1024},
        }


class TestParse:
    def test_openai_parse_keeps_order(self):
        """响应乱序返回时按 index 保序。"""
        payload = {"data": [
            {"index": 1, "embedding": [2.0, 2.0]},
            {"index": 0, "embedding": [1.0, 1.0]},
        ]}
        vectors = mp.OpenAICompatEmbedding().parse(payload, expected=2)
        assert vectors == [[1.0, 1.0], [2.0, 2.0]]

    def test_openai_parse_count_mismatch_raises(self):
        with pytest.raises(ValueError, match="数量不匹配"):
            mp.OpenAICompatEmbedding().parse(
                {"data": [{"index": 0, "embedding": [1.0]}]}, expected=2)

    def test_aliyun_parse_keeps_order(self):
        payload = {"output": {"embeddings": [
            {"text_index": 1, "embedding": [2.0]},
            {"text_index": 0, "embedding": [1.0]},
        ]}}
        vectors = mp.AliyunNativeEmbedding().parse(payload, expected=2)
        assert vectors == [[1.0], [2.0]]

    def test_aliyun_parse_count_mismatch_raises(self):
        with pytest.raises(ValueError, match="数量不匹配"):
            mp.AliyunNativeEmbedding().parse({"output": {"embeddings": []}}, expected=1)


def _bare_service(dim: int = 128) -> EmbeddingService:
    """绕过 __init__ 构造裸实例：不读全局配置、不建真实 HTTP 客户端。"""
    svc = EmbeddingService.__new__(EmbeddingService)
    svc.dim = dim
    svc.cfg = SimpleNamespace(max_retries=0)
    return svc


class TestLocalFallbackVector:
    """本地哈希降级向量：远端不可用时保证入库/检索链路不中断。"""

    def test_dim_and_normalized(self):
        svc = _bare_service(128)
        v = svc._local_vector("测试文本")
        assert v.shape == (128,)
        assert np.linalg.norm(v) == pytest.approx(1.0)

    def test_deterministic(self):
        """同文本恒得同向量（检索两侧必须一致）。"""
        svc = _bare_service(64)
        assert np.array_equal(svc._local_vector("同一段文本"), svc._local_vector("同一段文本"))

    def test_empty_text_not_zero_vector(self):
        svc = _bare_service(64)
        v = svc._local_vector("")
        assert v.shape == (64,)
        assert np.linalg.norm(v) == pytest.approx(1.0)


class TestEmbedBatch:
    """_embed_batch 端到端：MockTransport 验证报文并回灌响应。"""

    @staticmethod
    def _svc(handler, resolve_result):
        svc = _bare_service(dim=4)
        svc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def fake_resolve():
            return resolve_result
        svc._resolve = fake_resolve
        return svc

    @staticmethod
    def _run(svc, texts):
        async def _call():
            try:
                return await svc._embed_batch(texts)
            finally:
                await svc._client.aclose()
        return asyncio.run(_call())

    def test_aliyun_native_end_to_end(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"output": {"embeddings": [
                {"text_index": 1, "embedding": [2.0, 2.0, 2.0, 2.0]},
                {"text_index": 0, "embedding": [1.0, 1.0, 1.0, 1.0]},
            ]}})

        svc = self._svc(handler, (MAAS_NATIVE, "sk-test", "text-embedding-v4", False, "aliyun"))
        vectors = self._run(svc, ["甲", "乙"])

        # 报文：原生协议 input.texts + parameters
        assert seen["url"] == MAAS_NATIVE
        assert seen["auth"] == "Bearer sk-test"
        assert seen["body"]["input"] == {"texts": ["甲", "乙"]}
        assert seen["body"]["parameters"]["text_type"] == "document"
        # 响应乱序返回，解析保序
        assert [v.tolist() for v in vectors] == [[1.0] * 4, [2.0] * 4]
        assert vectors[0].dtype == np.float32

    def test_openai_compat_end_to_end(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"data": [
                {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]},
            ]})

        svc = self._svc(handler, (COMPAT, "sk-test", "m", False, "aliyun"))
        vectors = self._run(svc, ["甲"])

        assert seen["url"] == f"{COMPAT}/embeddings"
        assert seen["body"] == {"model": "m", "input": ["甲"],
                                "encoding_format": "float", "dimensions": 4}
        assert vectors[0].tolist() == [1.0, 0.0, 0.0, 0.0]

    def test_no_key_raises_runtime_error(self):
        """无密钥且非本地端点：拒绝远端调用，交给上层走本地降级。"""
        svc = self._svc(lambda r: httpx.Response(200, json={}),
                        (LOCAL, "", "m", False, "custom"))
        with pytest.raises(RuntimeError, match="未配置向量模型"):
            self._run(svc, ["x"])
