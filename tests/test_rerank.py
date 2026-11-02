"""重排序链路单元测试。

覆盖：
- 协议选择（阿里原生 / OpenAI 兼容）与端点补全
- 报文构造（阿里原生 input/parameters + instruct vs Jina 风格顶层字段）
- 响应解析（DashScope 嵌套 / 顶层 results / TEI 裸列表）
- RRF 降级融合排序
- _rerank_with_retry：TEI 字段兜底与阿里 400 不兜底（httpx.MockTransport）
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.services import model_protocols as mp
from app.services.rerank import (
    RerankService,
    build_rerank_body,
    is_aliyun_endpoint,
    is_dashscope_native,
    parse_rerank,
    rerank_endpoint,
    rrf_fusion,
)

DASHSCOPE_NATIVE = ("https://dashscope.aliyuncs.com"
                    "/api/v1/services/rerank/text-rerank/text-rerank")
MAAS_NATIVE = ("https://ws-xxx.cn-beijing.maas.aliyuncs.com"
               "/api/v1/services/rerank/text-rerank/text-rerank")
COMPAT = "https://dashscope.aliyuncs.com/compatible-mode/v1"
LOCAL = "http://127.0.0.1:8080"


class TestSelectRerankProtocol:
    """协议选择：按 URL 路径特征判定（修复 MaaS 域名不含 dashscope 被误判的历史 bug）。"""

    @pytest.mark.parametrize("url", [DASHSCOPE_NATIVE, MAAS_NATIVE])
    def test_native_path_selects_aliyun_native(self, url):
        assert mp.select_rerank_protocol(url).name == "aliyun_native"

    def test_compatible_mode_selects_openai(self):
        assert mp.select_rerank_protocol(COMPAT).name == "openai"

    def test_bare_aliyun_host_selects_native(self):
        """阿里系裸域名（无路径）按原生协议补全。"""
        url = "https://ws-xxx.cn-beijing.maas.aliyuncs.com"
        assert mp.select_rerank_protocol(url).name == "aliyun_native"

    @pytest.mark.parametrize("url", [LOCAL, f"{LOCAL}/rerank"])
    def test_local_selects_openai(self, url):
        assert mp.select_rerank_protocol(url).name == "openai"


class TestEndpoint:
    def test_local_appends_rerank(self):
        assert rerank_endpoint(LOCAL) == f"{LOCAL}/rerank"

    def test_full_path_kept(self):
        assert rerank_endpoint(MAAS_NATIVE) == MAAS_NATIVE
        assert rerank_endpoint(f"{LOCAL}/rerank") == f"{LOCAL}/rerank"

    def test_compatible_mode_appends_rerank(self):
        assert rerank_endpoint(COMPAT) == f"{COMPAT}/rerank"

    def test_native_api_v1_base_completed(self):
        assert mp.AliyunNativeRerank().endpoint(
            "https://dashscope.aliyuncs.com/api/v1") == DASHSCOPE_NATIVE


class TestBuildBody:
    def test_aliyun_native_body(self):
        body = mp.AliyunNativeRerank().build_body(
            "qwen3-rerank", "查询", ["文档A", "文档B"], 2, instruct="找出最相关文档")
        assert body == {
            "model": "qwen3-rerank",
            "input": {"query": "查询", "documents": ["文档A", "文档B"]},
            "parameters": {"top_n": 2, "return_documents": False,
                           "instruct": "找出最相关文档"},
        }

    def test_aliyun_native_body_without_instruct(self):
        body = mp.AliyunNativeRerank().build_body("m", "q", ["d"], 1)
        assert "instruct" not in body["parameters"]

    def test_openai_body_top_level_fields(self):
        body = mp.OpenAICompatRerank().build_body("m", "q", ["a", "b"], 2)
        assert body == {"model": "m", "query": "q", "documents": ["a", "b"], "top_n": 2}

    def test_texts_fallback_only_for_openai(self):
        """TEI 兜底报文：OpenAI 协议 documents→texts；阿里 400 不做改字段重试。"""
        openai = mp.OpenAICompatRerank()
        fallback = openai.texts_fallback_body(
            {"model": "m", "query": "q", "documents": ["a"], "top_n": 1}, "m")
        assert fallback == {"model": "m", "query": "q", "top_n": 1, "texts": ["a"]}
        assert mp.AliyunNativeRerank().texts_fallback_body({}, "m") is None


class TestParseRerank:
    def test_dashscope_nested(self):
        data = {"output": {"results": [
            {"index": 0, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.1}]}}
        assert parse_rerank(data) == [(0, 0.9), (1, 0.1)]

    def test_top_level_results(self):
        data = {"results": [{"index": 2, "score": 0.7}]}
        assert parse_rerank(data) == [(2, 0.7)]

    def test_tei_bare_list(self):
        data = [{"index": 1, "score": 0.8}, {"index": 0, "score": 0.2}]
        assert parse_rerank(data) == [(1, 0.8), (0, 0.2)]

    def test_invalid_entries_skipped(self):
        data = {"results": [{"index": 0}, {"score": 0.5}, "junk",
                            {"index": 1, "score": 0.3}]}
        assert parse_rerank(data) == [(1, 0.3)]

    def test_empty(self):
        assert parse_rerank({}) == []
        assert parse_rerank(None) == []


class TestRrfFusion:
    """RRF 降级精排：score(d) = Σ 1 / (k + rank_i(d))。"""

    def test_single_ranking(self):
        pairs = rrf_fusion([[10, 20, 30]], k=60, top_n=3)
        assert [d for d, _ in pairs] == [10, 20, 30]
        assert pairs[0][1] == pytest.approx(1 / 61)

    def test_multi_ranking_votes(self):
        """两路排名投票：doc2 在两路都靠前，总分最高。"""
        pairs = rrf_fusion([[1, 2, 3], [2, 3, 1]], k=60, top_n=3)
        assert pairs[0][0] == 2
        assert pairs[0][1] == pytest.approx(1 / 62 + 1 / 61)

    def test_top_n_truncation(self):
        assert len(rrf_fusion([[1, 2, 3, 4, 5]], k=60, top_n=2)) == 2

    def test_empty(self):
        assert rrf_fusion([], k=60, top_n=5) == []


class TestCompatibilityWrappers:
    """rerank.py 对外的兼容薄封装。"""

    def test_is_dashscope_native(self):
        assert is_dashscope_native(MAAS_NATIVE) is True
        assert is_dashscope_native(f"{LOCAL}/rerank") is False

    def test_is_aliyun_endpoint(self):
        assert is_aliyun_endpoint(COMPAT) is True
        assert is_aliyun_endpoint(LOCAL) is False

    def test_build_rerank_body_uses_global_instruct(self, monkeypatch):
        fake = SimpleNamespace(rerank=SimpleNamespace(instruct="检索指令"))
        monkeypatch.setattr("app.services.rerank.get_settings", lambda: fake)
        body = build_rerank_body(MAAS_NATIVE, "m", "q", ["d"], 1)
        assert body["parameters"]["instruct"] == "检索指令"


def _bare_service(handler, instruct: str = "") -> RerankService:
    """绕过 __init__ 构造裸实例，注入 MockTransport 客户端。"""
    svc = RerankService.__new__(RerankService)
    svc.cfg = SimpleNamespace(instruct=instruct)
    svc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return svc


def _run(svc: RerankService, *args):
    async def _call():
        try:
            return await svc._rerank_with_retry(*args)
        finally:
            await svc._client.aclose()
    return asyncio.run(_call())


class TestRerankWithRetry:
    """_rerank_with_retry 端到端（MockTransport）。"""

    def test_aliyun_native_request_and_parse(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"output": {"results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.1}]}})

        svc = _bare_service(handler, instruct="给定查询，找出最相关文档")
        pairs = _run(svc, "查询", ["文档A", "文档B"], 2,
                     MAAS_NATIVE, "qwen3-rerank", "sk-test")

        assert pairs == [(0, 0.9), (1, 0.1)]
        assert seen["url"] == MAAS_NATIVE
        assert seen["body"]["input"] == {"query": "查询", "documents": ["文档A", "文档B"]}
        assert seen["body"]["parameters"]["instruct"] == "给定查询，找出最相关文档"
        assert seen["body"]["parameters"]["return_documents"] is False

    def test_openai_tei_fallback_on_400(self):
        """TEI 只认 texts：400 时自动换字段重试一次。"""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            if "documents" in body:
                return httpx.Response(400, json={"error": "unknown field: documents"})
            return httpx.Response(200, json=[{"index": 1, "score": 0.8},
                                             {"index": 0, "score": 0.2}])

        svc = _bare_service(handler)
        pairs = _run(svc, "q", ["a", "b"], 2, f"{LOCAL}/rerank", "m", "")

        assert pairs == [(1, 0.8), (0, 0.2)]
        assert len(calls) == 2
        assert "documents" in calls[0] and "texts" in calls[1]

    def test_aliyun_400_no_fallback(self):
        """阿里的 400 是参数/鉴权问题：不做 TEI 式改字段重试，直接抛错。"""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(400, json={"code": "BadRequest.IllegalInput"})

        svc = _bare_service(handler)
        with pytest.raises(httpx.HTTPStatusError):
            _run(svc, "q", ["a"], 1, MAAS_NATIVE, "m", "sk-test")
        assert len(calls) == 1


class _FakeRerank:
    """返回固定 (下标, 分数) 的精排桩。"""

    def __init__(self, pairs):
        self._pairs = pairs

    async def rerank(self, query, documents, top_n=None):
        return self._pairs


def _chunks(texts: list[str]):
    from app.modules.rag.model import RetrievedChunk

    return [RetrievedChunk(chunk_id=str(i), text=t, score=0.02, document_id=1,
                           doc_version=1, title=t, source="vector")
            for i, t in enumerate(texts)]


class TestAggregateGating:
    """精排分数门控：概率分数不能再套 sigmoid，否则无关片段会混进引用。"""

    def _run(self, monkeypatch, scores, texts):
        from app.modules.rag import retrieval as r

        fake = SimpleNamespace(vector=SimpleNamespace(reranked_top_k=8, min_score=0.35))
        monkeypatch.setattr(r, "get_settings", lambda: fake)
        monkeypatch.setattr(r, "get_rerank",
                            lambda: _FakeRerank([(i, s) for i, s in enumerate(scores)]))
        return asyncio.run(r.aggregate("q", _chunks(texts)))

    def test_probability_scores_drop_irrelevant(self, monkeypatch):
        """阿里/Jina 返回 0~1 概率：0.22 / 0.05 这类无关片段必须被丢弃。"""
        out = self._run(monkeypatch, [0.99, 0.22, 0.05], ["人物传记", "ES教程", "并发编程"])
        assert [c.chunk_id for c in out] == ["0"]

    def test_logits_still_normalized_by_sigmoid(self, monkeypatch):
        """TEI/llama.cpp 返回可正可负 logits：仍走 sigmoid 归一后再门控。"""
        out = self._run(monkeypatch, [2.0, 0.5, -6.0], ["相关", "次相关", "无关"])
        assert [c.chunk_id for c in out] == ["0", "1"]

    def test_low_top1_means_no_relevant(self, monkeypatch):
        out = self._run(monkeypatch, [0.12, 0.05], ["a", "b"])
        assert out == []
