"""Embedding 服务（策略模式：协议差异封装在 model_protocols.EmbeddingProtocol）。

模型来源：后台「模型配置 → 用途=文本向量化」绑定的模型优先；
未配置时回退环境变量 LLM__EMBEDDING_MODEL。

弹性设计：
- 批量 + 重试 + 熔断（与 LLM 相同的 tenacity + CircuitBreaker 组合）
- 降级路径：远端不可用时使用本地"哈希 n-gram 投影"向量 ——
  无语义能力但保证入库/检索链路不中断（日志标注 fallback 防误当生产质量）
- 维度一致性：本地降级向量同样输出 cfg.embedding_dimensions 维

协议选择见 app.services.model_protocols.select_embedding_protocol：
- OpenAI 兼容（`/embeddings`，data[].embedding 保序）：OpenAI / 百炼兼容模式 / Ollama / vLLM
- 阿里原生（`/api/v1/services/embeddings/...`，output.embeddings 保序）：公有云与专属 MaaS
"""

from __future__ import annotations

import hashlib
import math

import httpx
import numpy as np
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.infra.circuit_breaker import get_breaker
from app.services.model_protocols import select_embedding_protocol

logger = get_logger("app.embedding")

_BATCH_SIZE = 10   # DashScope embeddings 单请求上限


class EmbeddingService:
    def __init__(self):
        cfg = get_settings().llm
        self.cfg = cfg
        self.dim = cfg.embedding_dimensions
        self._dim_key: tuple[str, str] | None = None   # 已探测维度对应的 (base_url, model)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_connections=10))
        self._breaker = get_breaker("embedding", failure_threshold=5, recovery_seconds=60)

    async def close(self) -> None:
        await self._client.aclose()

    async def probe_dim(self) -> int:
        """探测当前生效向量模型的真实输出维度；换模型后自动重探。

        不同模型维度不同（text-embedding-3=1536、embeddinggemma=768、bge-m3=1024），
        ES 索引按固定维度建，维度错配会让 KNN 直接 400，因此维度必须跟着模型走。
        探测失败（服务未起/无 Key）时沿用配置维度。
        """
        base_url, api_key, model_name, allow_empty, provider = await self._resolve()
        key = (base_url, model_name)
        if key == self._dim_key:
            return self.dim
        if not (api_key or allow_empty):
            self._dim_key = key
            return self.dim
        try:
            protocol = select_embedding_protocol(base_url)
            resp = await self._client.post(
                protocol.endpoint(base_url),
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                json=protocol.build_payload(model_name, ["dim-probe"], None, provider))
            resp.raise_for_status()
            vectors = protocol.parse(resp.json(), expected=1)
            emb = vectors[0] if vectors else []
            if emb and len(emb) != self.dim:
                logger.info("embedding_dim_detected", model=model_name,
                            dim=len(emb), was=self.dim, protocol=protocol.name)
                self.dim = len(emb)
        except Exception as e:
            logger.warning("embedding_dim_probe_failed", model=model_name,
                           error=str(e), dim=self.dim)
        self._dim_key = key
        return self.dim

    async def embed(self, texts: list[str]) -> list[np.ndarray]:
        """批量向量化（自动分批；整体失败时逐批降级本地向量）。"""
        await self.probe_dim()
        vectors: list[np.ndarray] = []
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i:i + _BATCH_SIZE]
            try:
                vectors.extend(await self._breaker.call(self._embed_batch, batch))
            except Exception as e:
                # 降级：本批使用本地哈希向量，保证入库流程不中断
                logger.warning("embedding_fallback_local", batch_size=len(batch), error=str(e))
                vectors.extend(self._local_vector(t) for t in batch)
        return vectors

    async def embed_one(self, text: str) -> np.ndarray:
        return (await self.embed([text]))[0]

    async def available(self) -> bool:
        """是否具备真实语义向量能力（后台绑定或环境变量配了 Key）。

        无 Key 时入库用本地哈希向量、查询侧若仍做 KNN 会与远端向量空间错配，
        因此检索层据此决定是否启用向量路。
        """
        base_url, api_key, model_name, allow_empty, provider = await self._resolve()
        return bool(api_key) or allow_empty

    async def _resolve(self) -> tuple[str, str, str, bool, str]:
        """解析当前生效的向量模型：(base_url, api_key, model_name, allow_empty, provider)。

        后台用途绑定优先；未绑定/绑定失效时回退环境变量配置。
        """
        from app.modules.model import service as model_service
        try:
            rt = await model_service.resolve_role_runtime("embedding")
        except Exception as e:      # 库不可用不应阻断入库：回退环境变量
            logger.warning("embedding_role_resolve_failed", error=str(e))
            rt = None
        if rt is None:
            cfg = self.cfg
            return cfg.base_url, cfg.api_key, cfg.embedding_model, False, "custom"
        return rt.base_url, rt.api_key, rt.model_name, rt.allow_empty_key, rt.provider

    # ---------------- 远端调用 ----------------
    async def _embed_batch(self, batch: list[str]) -> list[np.ndarray]:
        base_url, api_key, model_name, allow_empty, provider = await self._resolve()
        protocol = select_embedding_protocol(base_url)

        @retry(
            retry=retry_if_exception(lambda e: isinstance(
                e, (httpx.TimeoutException, httpx.ConnectError)) or
                getattr(e, "status_code", 0) in (429, 500, 502, 503)),
            wait=wait_random_exponential(multiplier=0.5, max=8),
            stop=stop_after_attempt(self.cfg.max_retries + 1),
            reraise=True,
        )
        async def _call() -> list[np.ndarray]:
            # 维度探测已确定 self.dim；支持维度参数的协议/供应商按该值固定输出，
            # 保证与 ES 索引维度一致（换模型时索引重建逻辑见 vector_store.ensure_index）
            payload = protocol.build_payload(model_name, batch, self.dim, provider)
            resp = await self._client.post(
                protocol.endpoint(base_url),
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                json=payload,
            )
            if resp.status_code in (429, 500, 502, 503):
                err = httpx.HTTPStatusError("embedding 依赖抖动", request=resp.request, response=resp)
                raise err
            resp.raise_for_status()
            vectors = protocol.parse(resp.json(), expected=len(batch))
            return [np.array(v, dtype=np.float32) for v in vectors]

        if not api_key and not allow_empty:
            raise RuntimeError("未配置向量模型 API Key，embedding 走本地降级向量")
        return await _call()

    # ---------------- 本地降级向量 ----------------
    def _local_vector(self, text: str) -> np.ndarray:
        """字符 2-gram 哈希投影（feature hashing）：同文本恒得同向量，L2 归一化。

        仅保障链路可用与"字面相似 ≈ 向量相似"，无深层语义能力。
        """
        vec = np.zeros(self.dim, dtype=np.float32)
        norm_text = text.lower().strip()
        grams = [norm_text[j:j + 2] for j in range(max(len(norm_text) - 1, 1))]
        for gram in grams:
            h = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
            idx = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vec[idx] += sign
        n = float(np.linalg.norm(vec))
        if n > 0:
            vec /= n
        else:
            # 空文本兜底：确定性方向向量，避免零向量导致余弦除零
            vec[0] = 1.0
        return vec


_service: EmbeddingService | None = None


def get_embedding() -> EmbeddingService:
    global _service
    if _service is None:
        _service = EmbeddingService()
    return _service


async def close_embedding() -> None:
    global _service
    if _service is not None:
        await _service.close()
        _service = None
