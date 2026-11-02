"""模型调用协议策略（Strategy 模式）。

不同供应商/部署形态的同一类模型，协议差异由策略对象封装，
调用方（embedding / rerank / 模型测试）只面向策略接口，
不再散布 `if "dashscope" in url` 之类的域名/路径判断。

协议族：
- EmbeddingProtocol：OpenAI 兼容 / 阿里 DashScope 原生（input.texts + parameters）
- RerankProtocol：   OpenAI 兼容（Jina 风格，含 llama.cpp/vLLM/Infinity/兼容模式）/
                     阿里原生（input + parameters）

选择依据：
- **URL 路径特征**（`/api/v1/services/embeddings` / `/api/v1/services/rerank`）判定阿里原生协议——
  公有云 dashscope 与专属 MaaS 域名（*.maas.aliyuncs.com）路径一致，按路径判定两类通吃；
- 其余一律视为 OpenAI 兼容（本地服务、百炼 compatible-mode 均走该协议）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

# 阿里原生协议路径标识与完整后缀
_EMBED_NATIVE_MARK = "/api/v1/services/embeddings"
_EMBED_NATIVE_SUFFIX = "/api/v1/services/embeddings/text-embedding/text-embedding"
_RERANK_NATIVE_MARK = "/api/v1/services/rerank"
_RERANK_NATIVE_SUFFIX = "/api/v1/services/rerank/text-rerank/text-rerank"

_ALIYUN_HOST_MARK = "aliyuncs.com"


# ------------------------------------------------------------------
# Embedding 协议
# ------------------------------------------------------------------
class EmbeddingProtocol(ABC):
    """向量化协议：构造端点与报文、解析响应为保序的向量列表。"""

    name: str = "abstract"

    @abstractmethod
    def endpoint(self, base_url: str) -> str:
        """由配置的服务地址推出完整请求地址。"""

    @abstractmethod
    def build_payload(self, model: str, texts: list[str],
                      dimensions: int | None, provider: str) -> dict:
        """构造请求体。dimensions=None 表示不显式指定输出维度。"""

    @abstractmethod
    def parse(self, payload: dict, expected: int) -> list[list[float]]:
        """解析响应为向量列表，长度必须与 expected（请求条数）一致且保序。"""


class OpenAICompatEmbedding(EmbeddingProtocol):
    """OpenAI 兼容协议：POST {base}/embeddings，data[].embedding 按 index 保序。

    覆盖：OpenAI、百炼 compatible-mode、Ollama、vLLM、本地兼容服务。
    """

    name = "openai"

    # 只有确定支持 dimensions 参数的供应商才传，避免本地/自定义端点 400
    _DIMENSIONS_PROVIDERS = ("aliyun", "bailian", "openai", "zhipu")

    def endpoint(self, base_url: str) -> str:
        base = (base_url or "").rstrip("/")
        return base if base.endswith("/embeddings") else f"{base}/embeddings"

    def build_payload(self, model: str, texts: list[str],
                      dimensions: int | None, provider: str) -> dict:
        payload: dict = {"model": model, "input": texts, "encoding_format": "float"}
        if dimensions and provider in self._DIMENSIONS_PROVIDERS:
            payload["dimensions"] = dimensions
        return payload

    def parse(self, payload: dict, expected: int) -> list[list[float]]:
        data = sorted(payload["data"], key=lambda d: d["index"])
        vectors = [d["embedding"] for d in data]
        if len(vectors) != expected:
            raise ValueError(f"embedding 数量不匹配: 请求 {expected} 条，返回 {len(vectors)} 条")
        return vectors


class AliyunNativeEmbedding(EmbeddingProtocol):
    """阿里 DashScope / 专属 MaaS 原生向量化协议。

    POST /api/v1/services/embeddings/text-embedding/text-embedding
    报文：{"model", "input": {"texts": [...]}, "parameters": {"text_type": "document"}}
    响应：output.embeddings[].embedding（按 text_index 保序）
    """

    name = "aliyun_native"

    def endpoint(self, base_url: str) -> str:
        base = (base_url or "").rstrip("/")
        if _EMBED_NATIVE_MARK in base:
            return base
        if _ALIYUN_HOST_MARK in base:
            if base.endswith("/api/v1"):
                return f"{base}/services/embeddings/text-embedding/text-embedding"
            return f"{base}{_EMBED_NATIVE_SUFFIX}"
        return f"{base}/embeddings"

    def build_payload(self, model: str, texts: list[str],
                      dimensions: int | None, provider: str) -> dict:
        # 原生协议按 text_type 区分检索两侧：入库 document / 查询 query 由调用方传，
        # 这里统一用 document，查询侧经 query 重载（见 embed_query 说明）
        params: dict = {"text_type": "document"}
        if dimensions:
            params["output_dimension"] = dimensions
        return {"model": model, "input": {"texts": texts}, "parameters": params}

    def parse(self, payload: dict, expected: int) -> list[list[float]]:
        items = sorted((payload.get("output") or {}).get("embeddings") or [],
                       key=lambda d: d["text_index"])
        vectors = [d["embedding"] for d in items]
        if len(vectors) != expected:
            raise ValueError(f"embedding 数量不匹配: 请求 {expected} 条，返回 {len(vectors)} 条")
        return vectors


def select_embedding_protocol(base_url: str) -> EmbeddingProtocol:
    """按 URL 路径特征选择向量化协议。"""
    if _EMBED_NATIVE_MARK in (base_url or ""):
        return AliyunNativeEmbedding()
    return OpenAICompatEmbedding()


# ------------------------------------------------------------------
# Rerank 协议
# ------------------------------------------------------------------
class RerankProtocol(ABC):
    """重排序协议：构造端点与报文。响应解析统一走 parse_rerank（各协议结构已兼容）。"""

    name: str = "abstract"

    @abstractmethod
    def endpoint(self, base_url: str) -> str: ...

    @abstractmethod
    def build_body(self, model: str, query: str,
                   documents: list[str], top_n: int, instruct: str = "") -> dict: ...

    def texts_fallback_body(self, body: dict, model: str) -> dict | None:
        """TEI 等实现只认 "texts" 字段：400/422 时的降级报文；不支持则返回 None。

        阿里端点必须返回 None——其 400 是参数/鉴权问题，改字段重试只会掩盖真实错误。
        """
        return None


class OpenAICompatRerank(RerankProtocol):
    """Jina 风格顶层字段协议：{"model","query","documents","top_n"}。

    覆盖：Jina、llama.cpp(--reranking)、vLLM、Infinity、百炼 compatible-mode。
    """

    name = "openai"

    def endpoint(self, base_url: str) -> str:
        base = (base_url or "").rstrip("/")
        if "rerank" in base:
            return base
        if "compatible-mode" in base:
            return f"{base}/rerank" if base.endswith("/v1") else f"{base}/v1/rerank"
        return f"{base}/rerank"

    def build_body(self, model: str, query: str,
                   documents: list[str], top_n: int, instruct: str = "") -> dict:
        return {"model": model, "query": query, "documents": documents, "top_n": top_n}

    def texts_fallback_body(self, body: dict, model: str) -> dict | None:
        if "documents" not in body:
            return None
        return {k: v for k, v in body.items() if k != "documents"} | \
            {"texts": body["documents"]}


class AliyunNativeRerank(RerankProtocol):
    """阿里 DashScope / 专属 MaaS 原生重排协议（input + parameters 包裹）。"""

    name = "aliyun_native"

    def endpoint(self, base_url: str) -> str:
        base = (base_url or "").rstrip("/")
        if "rerank" in base:
            return base
        if "compatible-mode" in base:
            # 百炼 OpenAI 兼容端点：该地址实际走 OpenAICompatRerank 报文，
            # 这里仅做兜底补全（选择器不会把 compatible-mode 判给原生）
            return f"{base}/rerank" if base.endswith("/v1") else f"{base}/v1/rerank"
        if base.endswith("/api/v1"):
            return f"{base}/services/rerank/text-rerank/text-rerank"
        return f"{base}{_RERANK_NATIVE_SUFFIX}"

    def build_body(self, model: str, query: str,
                   documents: list[str], top_n: int, instruct: str = "") -> dict:
        params: dict = {"top_n": top_n, "return_documents": False}
        if instruct:
            # qwen3-rerank 等模型可选：检索意图指令提升排序质量
            params["instruct"] = instruct
        return {"model": model,
                "input": {"query": query, "documents": documents},
                "parameters": params}

    def texts_fallback_body(self, body: dict, model: str) -> dict | None:
        return None   # 阿里 400 是参数/鉴权问题，不做 TEI 式改字段重试


def select_rerank_protocol(base_url: str) -> RerankProtocol:
    """按 URL 路径特征选择重排协议。

    原生路径命中原生协议；aliyuncs 域名下的裸地址按原生补全；
    其余（本地服务、compatible-mode）走 Jina 风格。
    """
    url = base_url or ""
    if _RERANK_NATIVE_MARK in url:
        return AliyunNativeRerank()
    if "compatible-mode" in url:
        return OpenAICompatRerank()
    if _ALIYUN_HOST_MARK in url:
        return AliyunNativeRerank()
    return OpenAICompatRerank()


def is_aliyun_host(url: str) -> bool:
    """是否阿里系域名（用于禁止 TEI 兜底重试等保护逻辑）。"""
    return _ALIYUN_HOST_MARK in (url or "")
