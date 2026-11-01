"""Rerank 精排服务（DashScope gte-rerank API）。

本服务不加载本地 ONNX 大模型（镜像/依赖过重），改用官方 Rerank API：
- API 失败/超时/熔断 → 降级为 RRF（Reciprocal Rank Fusion）融合多路召回排名，
  零外部依赖且仍是"多证据投票"，精排质量可接受。
"""

from __future__ import annotations

from typing import Sequence

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.infra.circuit_breaker import get_breaker

logger = get_logger("app.rerank")

# 本地 rerank 服务（llama.cpp --reranking / TEI / Infinity / vLLM）统一暴露 /rerank
_RERANK_PATH = "/rerank"

# 送精排前的体量约束：cross-encoder 按 (query, doc) 对吃上下文，
# 本地 llama.cpp 常以 -c 4096 启动，整段原文 × 几十条一起送会直接 500。
# 相关性判断靠前几百字已足够；条数过多也只是浪费算力（最终只取 top_n 条）。
_MAX_DOC_CHARS = 1000
_MAX_DOCS = 20


def rerank_endpoint(base_url: str) -> str:
    """把配置里的服务地址补成真正的 rerank 端点。

    DashScope 原生地址本身含 rerank，原样使用；本地服务通常只填根地址
    （如 http://localhost:8080），补 /rerank；vLLM/Jina 的 /v1 前缀同样适用。
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return get_settings().rerank.base_url
    return base if "rerank" in base else f"{base}{_RERANK_PATH}"


def build_rerank_body(url: str, model: str, query: str,
                      documents: list[str], top_n: int) -> dict:
    """按实现构造请求体：DashScope 用 input 包裹；其余走 Jina 风格（顶层 query/documents）。

    llama.cpp / vLLM / Infinity 都接受 Jina 风格；TEI 例外——它只认 "texts" 字段，
    由调用方在 400/422 时换 {"query", "texts"} 重试一次。
    """
    if "dashscope" in url:
        return {"model": model,
                "input": {"query": query, "documents": documents},
                "parameters": {"top_n": top_n, "return_documents": False}}
    return {"model": model, "query": query, "documents": documents, "top_n": top_n}


def parse_rerank(data) -> list[tuple[int, float]]:
    """统一解析各实现的 rerank 响应：[(下标, 分数)]。

    - DashScope：{"output": {"results": [{"index", "relevance_score"}]}}
    - TEI / Infinity / llama.cpp：直接返回 [{"index", "score"}] 或 {"results": [...]}
    """
    if isinstance(data, dict):
        results = ((data.get("output") or {}).get("results") or data.get("results")
                   or data.get("data") or [])
    else:
        results = data if isinstance(data, list) else []
    pairs: list[tuple[int, float]] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        idx = r.get("index")
        score = r.get("relevance_score", r.get("score"))
        if idx is None or score is None:
            continue
        pairs.append((int(idx), float(score)))
    return pairs



class RerankService:
    def __init__(self):
        cfg = get_settings().rerank
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.timeout_seconds, connect=3.0))
        self._breaker = get_breaker("rerank", failure_threshold=3, recovery_seconds=60)

    async def close(self) -> None:
        await self._client.aclose()

    async def rerank(self, query: str, documents: list[str],
                     top_n: int | None = None) -> list[tuple[int, float]] | None:
        """返回 [(原始下标, 相关性分数)] 降序；失败返回 None 由调用方走 RRF。"""
        if not documents or not self.cfg.enabled:
            return None
        top_n = top_n or self.cfg.top_n
        # 截断单条文本 + 限制送排条数，防本地 cross-encoder 上下文溢出（500）
        documents = [d[:_MAX_DOC_CHARS] for d in documents[:_MAX_DOCS]]
        # 后台「检索精排」用途绑定优先；未绑定回退环境变量精排配置
        rt = await self._resolve()
        if rt is not None:
            url = rerank_endpoint(rt.base_url)
            model = rt.model_name
            api_key = rt.api_key
            allow_empty = rt.allow_empty_key
        else:
            url = self.cfg.base_url
            model = self.cfg.model
            api_key = get_settings().llm.api_key
            allow_empty = False
        # 本地服务（llama.cpp/TEI 等）通常不校验密钥，允许空 Key 直连
        if not api_key and not allow_empty:
            logger.info("rerank_skipped", reason="未配置精排模型密钥，降级 RRF")
            return None
        try:
            return await self._breaker.call(self._rerank_with_retry,
                                            query, documents, top_n, url, model, api_key)
        except Exception as e:
            logger.warning("rerank_failed_fallback_rrf", error=str(e), docs=len(documents))
            return None

    async def _resolve(self):
        """解析当前生效的精排模型；失败返回 None（回退环境变量配置）。"""
        from app.modules.model import service as model_service
        try:
            return await model_service.resolve_role_runtime("rerank")
        except Exception as e:
            logger.warning("rerank_role_resolve_failed", error=str(e))
            return None

    async def _rerank_with_retry(self, query: str, documents: list[str], top_n: int,
                                 url: str, model: str, api_key: str):

        @retry(
            retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
            wait=wait_random_exponential(multiplier=0.3, max=4),
            stop=stop_after_attempt(2),   # 精排容忍度低：快重试快降级，不拖长首Token延迟
            reraise=True,
        )
        async def _call() -> list[tuple[int, float]]:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            body = build_rerank_body(url, model, query, documents, top_n)
            resp = await self._client.post(url, headers=headers, json=body)
            # TEI 只认 "texts" 不认 "documents"：校验失败时切换字段重试一次
            if resp.status_code in (400, 422) and "documents" in body:
                body = {k: v for k, v in body.items() if k != "documents"} | {"texts": documents}
                resp = await self._client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            pairs = parse_rerank(resp.json())
            if not pairs:      # 空结果按失败处理，交给上层降级 RRF
                raise RuntimeError("rerank 响应中没有可解析的分数")
            return pairs

        return await _call()


def rrf_fusion(rankings: Sequence[Sequence[int]], k: int = 60,
               top_n: int = 5) -> list[tuple[int, float]]:
    """RRF（Reciprocal Rank Fusion）降级精排。

    score(d) = Σ 1 / (k + rank_i(d))，多路召回排名投票；
    k=60 为信息检索领域经验常数，抑制头部排名的过度主导。
    返回 [(doc_id, rrf_score)] 降序。
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])[:top_n]


_service: RerankService | None = None


def get_rerank() -> RerankService:
    global _service
    if _service is None:
        _service = RerankService()
    return _service
