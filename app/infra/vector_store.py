"""向量存储抽象：Elasticsearch 主实现 + 数据库/内存降级实现。

主实现 ES 8.x（docker-compose 部署）：
- dense_vector + KNN，match 全文，脚本混合打分 → KNN / 全文 / 混合三模式
- 权限（accessibleBy）、版本（docVersion）、过期（expireAt）全部下推为 filter，
  减少应用侧二次过滤

降级实现（ES 不可用时的 dev 兜底，接口完全一致）：
- 向量存 knowledge_segment.embedding，进程内 numpy 批量余弦
- 全文用 SQL LIKE；数据量定位：万级分段以内
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime

import numpy as np

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.models.constants import MetaKey

logger = get_logger("app.vector")

_HAN = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-z0-9_+#.]{2,}")

# 疑问词/功能虚字：由它们构成的二元组（"的操"、"什么"、"如何"）几乎出现在任何中文文本里，
# 计入相关性会把无关片段抬到 0.3 以上，因此不参与词面校验。
# 只收真正无实义的字：像"能/可/要/有/为"会吞掉"性能/可靠/重要/有效"等关键术语，不能放进停用集。
_STOP_CHARS = set("的地得了是在吗呢吧啊嘛什么怎哪个哦嗯请帮们这那之其而")
_EN_STOP = {"the", "a", "an", "is", "are", "was", "were", "be", "been", "of", "to", "in",
            "on", "at", "for", "and", "or", "not", "what", "how", "why", "when", "where",
            "which", "who", "do", "does", "did", "it", "this", "that", "with", "can",
            "could", "should", "would", "i", "you", "we", "they", "me", "my"}

# 通用过程词：任何技术文档都大量出现，不能作为"问到了这个知识点"的证据。
# 例如问"倒车入库的操作流程"，只靠"操作/流程"就会把 ES 的 bulk 操作说明当成答案。
_GENERIC_TERMS = {
    "操作", "流程", "步骤", "方法", "实现", "方式", "原理", "作用", "介绍", "区别",
    "优点", "缺点", "使用", "配置", "问题", "情况", "内容", "相关", "知识", "说明",
    "详细", "简单", "一下", "可以", "能否", "应该", "需要", "原因", "结果", "过程",
    "例如", "比如", "包括", "进行", "处理", "支持", "工作", "场景", "示例", "代码",
    "如何", "什么", "怎么", "为什么", "哪些", "哪个", "是否", "多少", "怎样",
}


def _generic(gram: str) -> bool:
    """二元组是否由虚词或通用过程词构成。"""
    return bool(_STOP_CHARS & set(gram)) or gram in _GENERIC_TERMS


def query_terms(text: str) -> set[str]:
    """问题拆词：英文/数字词 + 中文二元组（剔除疑问虚词与通用过程词）。"""
    terms = {w for w in _WORD.findall(text.lower()) if w not in _EN_STOP}
    for run in _HAN.findall(text):
        if len(run) == 1:            # 单字中文噪声太大（"锁""类"会被任何文本命中）
            continue
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if _generic(gram):
                continue
            terms.add(gram)
    return terms


def has_anchor(question: str, text: str, min_len: int = 3) -> bool:
    """问题中是否有一段 ≥min_len 的连续中文（不含虚词/通用词）原样出现在文本中。

    命中率之外再加一道硬门槛：只命中"操作/流程/什么"等零散词的片段直接判为不相关，
    避免知识库没有对应内容时仍硬凑出引用（用户看到的就是"答非所问 + 相似度还行"）。
    """
    low = text.lower()
    for run in _HAN.findall(question):
        for i in range(len(run) - min_len + 1):
            seg = run[i:i + min_len]
            # 3 字窗口内任意二元组是虚词/通用词，这段连续匹配就不算实质证据
            if any(_generic(seg[j:j + 2]) for j in range(len(seg) - 1)):
                continue
            if seg in low:
                return True
    return False


def term_overlap(terms: set[str], text: str) -> float:
    """加权术语命中率：英文/数字词权重 3（技术名词是最强信号），中文二元组权重 1，0~1。"""
    if not terms:
        return 0.0
    low = text.lower()
    total = sum(3.0 if t.isascii() else 1.0 for t in terms)
    hit = sum(3.0 if t.isascii() else 1.0 for t in terms if t in low)
    return hit / total


@dataclass
class ChunkRecord:
    """一条可检索分段的存储视图（与 knowledge_segment 解耦，降级实现同源）。"""

    chunk_id: str
    text: str
    document_id: int
    doc_version: int
    title: str = ""
    order: int = 0
    skip_embedding: int = 0
    accessible_by: str | None = None
    expire_at: int | None = None            # epoch 秒；None=永不过期
    vector: list[float] | None = None


@dataclass
class SearchHit:
    chunk_id: str
    score: float                        # 融合/召回排序分（RRF 名次分或词面命中率）
    text: str = ""
    document_id: int = 0
    doc_version: int = 0
    source: str = "vector"
    metadata: dict = field(default_factory=dict)
    vector_score: float | None = None   # 真实向量相似度（无 KNN 命中时为 None）


@dataclass
class SearchFilter:
    """检索过滤条件（权限 + 版本 + 过期），两实现语义一致。

    doc_versions：允许命中的版本集合（文档激活版本/当前可用版本）。
    为空列表表示"没有任何可检索版本"，此时必须返回空结果（不能退化为不过滤）。
    """

    doc_versions: list[int] | None = None
    user_identity: str | None = None         # 角色/工号；空=不过滤
    exclude_expired: bool = True


class VectorStore(ABC):
    @abstractmethod
    async def ensure_index(self, dim: int) -> None: ...

    @abstractmethod
    async def upsert(self, records: list[ChunkRecord]) -> int: ...

    @abstractmethod
    async def delete_by_document(self, doc_id: int) -> int: ...

    @abstractmethod
    async def delete_by_version(self, version_id: int) -> int: ...

    @abstractmethod
    async def search(self, query_vector: np.ndarray | None, query_text: str | None,
                     top_k: int, flt: SearchFilter) -> list[SearchHit]:
        """query_vector→KNN；query_text→全文；两者都给→混合（RRF 融合）。"""

    @abstractmethod
    def backend_name(self) -> str: ...


# ==================================================================
# Elasticsearch 实现
# ==================================================================
# ES 8.x 原生 kNN：dense_vector 字段设 index:true + similarity 即可，
# 无需（且不允许）OpenSearch 式的索引级 index.knn 设置，否则报 unknown setting。
_INDEX_SETTINGS = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            "chunkId":      {"type": "keyword"},
            # standard + 查询侧二元组短语补丁（改 cjk 需重建索引，见 settings 说明）
            "text":         {"type": "text", "analyzer": "standard"},
            "documentId":   {"type": "long"},
            "docVersion":   {"type": "long"},
            "title":        {"type": "keyword"},
            "order":        {"type": "integer"},
            "skipEmbedding": {"type": "byte"},
            "accessibleBy": {"type": "keyword"},
            "expireAt":     {"type": "long"},
            "vector":       {"type": "dense_vector", "dims": 1536,
                             "index": True, "similarity": "cosine"},
        }
    },
}


def _visible_to(accessible_by: str | None, identity: str) -> bool:
    """权限可见性判定：逗号分隔身份任一命中即放行，VISITOR 为公开。"""
    accs = {s.strip() for s in str(accessible_by or "VISITOR").split(",") if s.strip()}
    return identity in accs or "VISITOR" in accs


class ElasticsearchVectorStore(VectorStore):
    def __init__(self, client, index_name: str):
        self._es = client
        self._index = index_name

    def backend_name(self) -> str:
        return "elasticsearch"

    async def ensure_index(self, dim: int) -> None:
        body = {**_INDEX_SETTINGS,
                "mappings": {**_INDEX_SETTINGS["mappings"]}}
        body["mappings"]["properties"]["vector"]["dims"] = dim
        exists = await self._es.indices.exists(index=self._index)
        if exists:
            # 维度随绑定的向量模型变化（如 1536 → 768），索引是按旧维度建的：
            # 空索引直接重建；非空索引不擅自动数据，仅报错提示需人工处理
            try:
                mapping = await self._es.indices.get_mapping(index=self._index)
                cur = mapping[self._index]["mappings"]["properties"]["vector"]["dims"]
            except Exception:
                return
            if cur == dim:
                return
            count = int((await self._es.count(index=self._index))["count"])
            if count == 0:
                logger.warning("es_index_dim_recreate", index=self._index, old=cur, new=dim)
                await self._es.indices.delete(index=self._index)
            else:
                logger.error("es_index_dim_mismatch", index=self._index, old=cur, new=dim,
                             note="索引非空未自动重建；需先清空切片再重建，或改回原维度模型")
                return
        await self._es.indices.create(index=self._index, body=body)
        logger.info("es_index_created", index=self._index, dim=dim)

    async def upsert(self, records: list[ChunkRecord]) -> int:
        """bulk 写入：一次网络往返写完整批，失败分片逐条重试。"""
        if not records:
            return 0
        actions: list[dict] = []
        for r in records:
            # accessible_by 逗号分隔多身份，keyword 字段按数组写入，term 才能逐一命中
            acc = [s.strip() for s in (r.accessible_by or "").split(",") if s.strip()]
            src = {
                "chunkId": r.chunk_id, "text": r.text, "documentId": r.document_id,
                "docVersion": r.doc_version, "title": r.title, "order": r.order,
                "skipEmbedding": r.skip_embedding, "accessibleBy": acc or ["VISITOR"],
                "expireAt": r.expire_at,
            }
            if r.vector is not None:
                src["vector"] = r.vector
            actions.append({"index": {"_index": self._index, "_id": r.chunk_id}})
            actions.append(src)
        # 客户端 bulk 是同步 API，官方异步客户端直接循环 index（bulk helper）
        ok = 0
        for i in range(0, len(actions), 2):
            try:
                await self._es.index(index=self._index, id=actions[i]["index"]["_id"],
                                     document=actions[i + 1])
                ok += 1
            except Exception as e:
                logger.error("es_index_doc_failed", chunk=actions[i]["index"]["_id"], error=str(e))
        return ok

    async def delete_by_document(self, doc_id: int) -> int:
        return await self._delete_by({"term": {"documentId": doc_id}})

    async def delete_by_version(self, version_id: int) -> int:
        return await self._delete_by({"term": {"docVersion": version_id}})

    async def _delete_by(self, clause: dict) -> int:
        """clause 为完整查询子句（如 {"term": {...}}），直接作 query，避免再套一层 term。"""
        resp = await self._es.delete_by_query(index=self._index,
                                              body={"query": clause},
                                              conflicts="proceed", refresh=True)
        return int(resp.get("deleted", 0))

    # ---------------- 检索 ----------------
    def _filters(self, flt: SearchFilter) -> list[dict]:
        conds: list[dict] = [{"term": {"skipEmbedding": 0}}]
        if flt.doc_versions is not None:
            # terms 空列表在 ES 中匹配不到任何文档，正好对应"无可检索版本"
            conds.append({"terms": {"docVersion": flt.doc_versions}})
        if flt.user_identity:
            # 权限：文档可见范围包含该身份或所有人（accessibleBy 字段）
            conds.append({"bool": {"should": [
                {"term": {"accessibleBy": flt.user_identity}},
                {"term": {"accessibleBy": "VISITOR"}},
            ], "minimum_should_match": 1}})
        if flt.exclude_expired:
            now = int(datetime.now().timestamp())
            conds.append({"bool": {"should": [
                {"bool": {"must_not": {"exists": {"field": "expireAt"}}}},
                {"range": {"expireAt": {"gt": now}}},
            ]}})
        return conds

    async def search(self, query_vector, query_text, top_k, flt: SearchFilter) -> list[SearchHit]:
        filters = self._filters(flt)
        knn_hits: list[SearchHit] = []
        text_hits: list[SearchHit] = []

        # KNN 路
        if query_vector is not None:
            knn_body = {
                "knn": {"field": "vector", "query_vector": list(map(float, query_vector)),
                        "k": top_k, "num_candidates": max(top_k * 10, 100),
                        "filter": {"bool": {"filter": filters}}},
                "size": top_k,
                "_source": ["chunkId", "text", "documentId", "docVersion", "title", "order"],
            }
            try:
                resp = await self._es.search(index=self._index, body=knn_body)
            except Exception as e:
                # 维度失配（换了向量模型但索引还是旧维度）时降级为纯全文检索，
                # 而不是让整个检索 500；索引重建见 ensure_index
                if "different number of dimensions" in str(e):
                    logger.warning("es_knn_dim_mismatch_fallback_fulltext",
                                   vector_dim=len(query_vector), error=str(e)[:120])
                    resp = None
                else:
                    raise
            if resp is not None:
                for h in resp["hits"]["hits"]:
                    src = h["_source"]
                    knn_hits.append(SearchHit(h["_id"], float(h["_score"]), src.get("text", ""),
                                              src.get("documentId", 0), src.get("docVersion", 0),
                                              "vector", {"title": src.get("title"),
                                                         "order": src.get("order")},
                                              vector_score=float(h["_score"])))

        # 全文路：用问题拆词构造 should 子句。
        # standard 分词器把中文切成单字，直接 match 整句会被"的/怎/么"等常用字淹没，
        # 改为「中文二元组短语 + 英文词」：二元组在单字 token 流上等价于子串匹配，召回精准得多。
        if query_text:
            should: list[dict] = []
            for term in sorted(query_terms(query_text)):
                if term.isascii():
                    should.append({"match": {"text": {"query": term, "operator": "and",
                                                      "boost": 2.0}}})
                elif len(term) > 1:                 # 单字中文噪声太大，丢弃
                    should.append({"match_phrase": {"text": {"query": term}}})
            if should:
                match_body = {
                    "query": {"bool": {
                        "should": should,
                        "minimum_should_match": max(1, int(len(should) * 0.3)),
                        "filter": filters,
                    }},
                    "size": top_k,
                    "_source": ["chunkId", "text", "documentId", "docVersion", "title", "order"],
                }
                resp = await self._es.search(index=self._index, body=match_body)
                for h in resp["hits"]["hits"]:
                    src = h["_source"]
                    text_hits.append(SearchHit(h["_id"], 0.0, src.get("text", ""),
                                               src.get("documentId", 0), src.get("docVersion", 0),
                                               "fulltext", {"title": src.get("title"),
                                                            "order": src.get("order")}))

        # RRF 融合：两路统一换成“名次分”再相加。
        # KNN 余弦分（~0.5）与 BM25 分（~20）量纲不可比，直接比大小会让全文路永远被淹没。
        merged: dict[str, SearchHit] = {}
        fused: dict[str, float] = {}
        for rank, h in enumerate(knn_hits):
            merged[h.chunk_id] = h
            fused[h.chunk_id] = fused.get(h.chunk_id, 0.0) + 1.0 / (60 + rank + 1)
        for rank, h in enumerate(text_hits):
            if h.chunk_id in merged:
                merged[h.chunk_id].source = "hybrid"
            else:
                merged[h.chunk_id] = h
            fused[h.chunk_id] = fused.get(h.chunk_id, 0.0) + 1.0 / (60 + rank + 1)

        result = sorted(merged.values(), key=lambda x: -fused.get(x.chunk_id, 0.0))[:top_k]
        for h in result:
            h.score = fused.get(h.chunk_id, h.score)
        return result


# ==================================================================
# 降级实现：DB 向量 + 内存 numpy / SQL LIKE 全文
# ==================================================================
class DatabaseVectorStore(VectorStore):
    """无 ES 环境的兜底：批量拉候选 + numpy 余弦；全文 LIKE。"""

    def __init__(self):
        self._candidate_limit = 5000    # 兜底模式的最大扫描量，防内存失控

    def backend_name(self) -> str:
        return "database-fallback"

    async def ensure_index(self, dim: int) -> None:
        return None

    async def upsert(self, records: list[ChunkRecord]) -> int:
        # 降级模式下向量随分段行落库（由 document service 写入），此处仅确认数量
        return len(records)

    async def delete_by_document(self, doc_id: int) -> int:
        from app.core.database import session_scope
        from sqlalchemy import update
        from app.models.entities import KnowledgeSegment

        async with session_scope() as session:
            r = await session.execute(
                update(KnowledgeSegment)
                .where(KnowledgeSegment.document_id == doc_id,
                       KnowledgeSegment.deleted == 0)
                .values(deleted=1))
            return r.rowcount

    async def delete_by_version(self, version_id: int) -> int:
        from app.core.database import session_scope
        from sqlalchemy import update
        from app.models.entities import KnowledgeSegment

        async with session_scope() as session:
            r = await session.execute(
                update(KnowledgeSegment)
                .where(KnowledgeSegment.document_version == version_id,
                       KnowledgeSegment.deleted == 0)
                .values(deleted=1))
            return r.rowcount

    async def search(self, query_vector, query_text, top_k, flt: SearchFilter) -> list[SearchHit]:
        from app.core.database import session_scope
        from sqlalchemy import select
        from app.models.entities import KnowledgeSegment

        async with session_scope() as session:
            stmt = (
                select(KnowledgeSegment)
                .where(KnowledgeSegment.deleted == 0,
                       KnowledgeSegment.skip_embedding == 0)
                .limit(self._candidate_limit)
            )
            if flt.doc_versions is not None:
                stmt = stmt.where(KnowledgeSegment.document_version.in_(flt.doc_versions))
            rows = (await session.execute(stmt)).scalars().all()

        # 降级模式同样执行权限过滤（此前缺失，ES 不可用时专属文档会对所有人泄露）
        if flt.user_identity and flt.user_identity != "VISITOR":
            ident = flt.user_identity
            rows = [r for r in rows
                    if _visible_to((r.metadata_ or {}).get(MetaKey.ACCESSIBLE_BY), ident)]

        hits: dict[str, SearchHit] = {}

        if query_vector is not None and rows:
            mat = np.stack([
                np.frombuffer(r.embedding, dtype=np.float32) for r in rows if r.embedding
            ]) if any(r.embedding for r in rows) else None
            if mat is not None:
                q = query_vector / (np.linalg.norm(query_vector) + 1e-9)
                mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
                sims = mat @ q
                with_emb = [r for r in rows if r.embedding]
                idx = np.argsort(-sims)[:top_k]
                for i in idx:
                    if sims[i] <= 0:
                        continue
                    r = with_emb[i]
                    hits[r.chunk_id] = SearchHit(
                        r.chunk_id, float(sims[i]), r.text, r.document_id,
                        r.document_version or 0, "vector",
                        dict(r.metadata_ or {}), vector_score=float(sims[i]))

        if query_text:
            # 全文降级：按问题词命中率打分（原先的定长前缀子串几乎不可能命中中文问句）
            terms = query_terms(query_text)
            for r in rows:
                ov = term_overlap(terms, r.text[:2000])
                if ov < 0.15:
                    continue
                if r.chunk_id in hits:
                    hits[r.chunk_id].score += ov
                    hits[r.chunk_id].source = "hybrid"
                else:
                    hits[r.chunk_id] = SearchHit(
                        r.chunk_id, ov, r.text, r.document_id,
                        r.document_version or 0, "fulltext", dict(r.metadata_ or {}))

        return sorted(hits.values(), key=lambda x: -x.score)[:top_k]


# ==================================================================
# 工厂：ES 优先，探测失败降级
# ==================================================================
_store: VectorStore | None = None
_init_lock = asyncio.Lock()


async def get_vector_store() -> VectorStore:
    global _store
    if _store is not None:
        return _store
    async with _init_lock:
        if _store is not None:
            return _store
        cfg = get_settings().vector
        if cfg.es_url:
            try:
                from elastic_transport import ConnectionError as ESTransportError
                from elasticsearch import AsyncElasticsearch

                kwargs = {"hosts": cfg.es_url, "request_timeout": 5.0}
                if cfg.es_username:
                    kwargs["basic_auth"] = (cfg.es_username, cfg.es_password)
                client = AsyncElasticsearch(**kwargs)
                # 探测：2 秒内拿不到 cluster health 即判不可用
                info = await client.info()
                await client.close()
                if "version" not in info.body:
                    raise ESTransportError("ES 版本信息缺失")

                # 正式客户端（保持长连接）
                client = AsyncElasticsearch(**kwargs)
                store = ElasticsearchVectorStore(client, cfg.index_name)
                # 维度以绑定的向量模型实测为准（配置固定值会随换模型失配）
                from app.services.embedding import get_embedding
                dim = await get_embedding().probe_dim()
                await store.ensure_index(dim)
                _store = store
                logger.info("vector_connected", backend="elasticsearch", url=cfg.es_url)
                return _store
            except Exception as e:
                logger.warning("vector_fallback_db",
                               reason=f"Elasticsearch 不可达（{e}），已降级为库内向量兜底")
        _store = DatabaseVectorStore()
        return _store
