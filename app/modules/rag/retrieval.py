"""混合检索器：向量 KNN + 全文，父子分段回溯，Rerank/RRF 聚合。

用显式异步函数组织向量 KNN、全文检索与结果聚合：

1. retrieve(): 调 vector_store.search（KNN+全文混合，权限/版本/过期 filter 下推）
2. 父子分段回溯: 命中子分段用 parentChunkId 替换为父分段全文（上下文完整），
   同一父分段去重（父分段已含全部子分段）
3. aggregate(): DashScope Rerank 精排，失败/未启用降级 RRF 多路排名融合
"""

from __future__ import annotations

import math

from sqlalchemy import select

from app.config.settings import get_settings
from app.core.database import session_scope
from app.core.logging import get_logger
from app.core.tenant import get_current_tenant
from app.infra.vector_store import (SearchFilter, get_vector_store, has_anchor,
                                    query_terms, term_overlap)
from app.models.constants import MetaKey
from app.models.entities import KnowledgeSegment
from app.modules.document.service import get_segment_text_by_chunk_id
from app.modules.rag.model import RagReference, RetrievedChunk
from app.services.embedding import get_embedding
from app.services.rerank import get_rerank

logger = get_logger("app.rag.retrieval")

# 无精排时的核心词命中率下限（精排场景由下方 _SIG_TOP1_FLOOR / _SIG_REL_MARGIN 门控）
_MIN_TERM_OVERLAP = 0.4

# 精排门控参数。
# llama.cpp / TEI 返回的是 Cross-Encoder 原始 logits（可正可负，量纲随模型变化），
# 不能直接和固定阈值比较：同一批候选里 top1 分数高时，同档相关片段也可能为负值。
# 因此先用 sigmoid 归一成 0~1 概率，再做两道门控：
#   TOP1_FLOOR —— 整批最高分都低于该概率，判为"没有相关资料"，返回空（如"倒车入库"top1 logit≈-6.5）
#   REL_MARGIN —— 保留与 top1 概率差距在 0.35 以内的片段，避免绝对阈值误杀次优的相关结果
_SIG_TOP1_FLOOR = 0.30
_SIG_REL_MARGIN = 0.35

# 阿里 / Jina / vLLM 直接返回 0~1 的 relevance_score（已是概率），不能再套 sigmoid
# （sigmoid(0.99)=0.73、sigmoid(0.2)=0.55，会把阈值门控彻底压平，无关片段全部混进引用）。
# 概率分支使用更严格的相对差距 + 绝对下限：
_PROB_TOP1_FLOOR = 0.30      # 最高分都低于此值 → 没有相关资料
_PROB_REL_MARGIN = 0.50      # 概率尺度上只保留与 top1 差距 0.5 以内的片段
_PROB_MIN_KEEP = 0.20        # 绝对下限：精排认为基本不相关的片段一律不进引用

# 邻接兜底参数。
# 非标准文档里"题目"与"答案"常被切成相邻两段（或历史数据已经这样入库），
# 只召回其中一段会出现"引用只有标题没有正文"。命中分段过短时补同文档 order±1，
# 让旧数据不重新切片也能得到完整上下文。
_NEIGHBOR_MIN_CHARS = 200      # 命中分段正文短于该长度才补邻居
_NEIGHBOR_MAX_CHARS = 900      # 补全后的分段长度上限（避免把整章塞进上下文）


def _sigmoid(x: float) -> float:
    """logits → 0~1 概率（防溢出）。"""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


async def _load_metadata(chunk_ids: list[str]) -> dict[str, dict]:
    """批量取分段 metadata（父子回溯需要 parentChunkId/fileName），走库内 JSON 列。"""
    if not chunk_ids:
        return {}
    async with session_scope() as session:
        rows = (await session.execute(
            select(KnowledgeSegment.chunk_id, KnowledgeSegment.metadata_,
                   KnowledgeSegment.text).where(
                KnowledgeSegment.chunk_id.in_(chunk_ids),
                KnowledgeSegment.deleted == 0))).all()
    return {r.chunk_id: {"metadata": r.metadata_ or {}, "text": r.text} for r in rows}


async def _expand_short_neighbors(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """命中分段过短时补同文档相邻分段（order-1 / order+1）。

    只处理"没有父分段"的普通分段：父分段回溯后本身已是完整章节，
    再补邻居会把同一章内容重复注入。相邻分段已在结果集中时跳过，避免重复。
    每个文档版本只查一次库，代价可控。
    """
    targets = [c for c in chunks
                if len(c.text.strip()) < _NEIGHBOR_MIN_CHARS
                and not c.metadata.get(MetaKey.PARENT_CHUNK_ID)
                and c.metadata.get(MetaKey.ORDER) is not None]
    if not targets:
        return chunks

    by_version: dict[int, list[RetrievedChunk]] = {}
    for c in targets:
        by_version.setdefault(c.doc_version, []).append(c)

    texts: dict[tuple[int, int], str] = {}
    ids: dict[tuple[int, int], str] = {}
    existing = {c.chunk_id for c in chunks}
    for version, items in by_version.items():
        orders = [int(c.metadata[MetaKey.ORDER]) for c in items]
        async with session_scope() as session:
            rows = (await session.execute(
                select(KnowledgeSegment.chunk_order, KnowledgeSegment.text,
                       KnowledgeSegment.chunk_id).where(
                    KnowledgeSegment.document_version == version,
                    KnowledgeSegment.deleted == 0,
                    KnowledgeSegment.skip_embedding == 0,
                    KnowledgeSegment.chunk_order.between(min(orders) - 1, max(orders) + 1)))).all()
        for order, text, chunk_id in rows:
            texts[(version, order)] = text
            ids[(version, order)] = chunk_id

    expanded = 0
    for c in targets:
        version, order = c.doc_version, int(c.metadata[MetaKey.ORDER])
        pieces = [c.text.strip()]
        for neighbor_order in (order - 1, order + 1):
            chunk_id = ids.get((version, neighbor_order))
            piece = texts.get((version, neighbor_order))
            if not piece or chunk_id in existing:
                continue
            if neighbor_order < order:
                pieces.insert(0, piece.strip())
            else:
                pieces.append(piece.strip())
        merged = "\n\n".join(p for p in pieces if p)
        if merged and len(merged) > len(c.text):
            c.text = merged[:_NEIGHBOR_MAX_CHARS]
            c.metadata["neighborExpanded"] = True
            expanded += 1

    if expanded:
        logger.info("neighbor_expanded", total=len(chunks), expanded=expanded)
    return chunks


async def retrieve(query_text: str, *, user_identity: str | None = None,
                   doc_versions: list[int] | None = None) -> list[RetrievedChunk]:
    """混合召回 + 父子分段回溯，返回按召回分降序的分段列表。

    doc_versions：允许命中的版本集合；由调用方传入"当前可用版本"，
    避免旧版本/已下线版本的分段污染检索结果。
    """
    cfg = get_settings().vector
    store = await get_vector_store()

    if doc_versions is None:
        # 未显式指定时统一按"当前可用版本"过滤，避免旧版本/已删除版本的分段被召回
        from app.modules.document import service as document_service
        doc_versions = await document_service.active_version_ids()

    # 1. 查询向量化（模型来自后台「文本向量化」用途绑定或环境变量）
    #    没有可用向量模型时本地哈希向量与入库向量空间错配，KNN 纯噪声，只走全文路
    embedding = get_embedding()
    query_vector = await embedding.embed_one(query_text) if await embedding.available() else None

    flt = SearchFilter(doc_versions=doc_versions, user_identity=user_identity,
                       tenant_id=get_current_tenant())
    # 向量 + 全文两路一起交给 store（ES 内部做混合；降级库内做 numpy 余弦 + LIKE）
    hits = await store.search(query_vector, query_text, cfg.chunk_top_k, flt)

    # 2. 补元数据 + 父子回溯
    max_parent = max(200, cfg.max_context_chars)
    metas = await _load_metadata([h.chunk_id for h in hits])
    chunks: list[RetrievedChunk] = []
    seen_parents: set[str] = set()
    for h in hits:
        info = metas.get(h.chunk_id, {})
        meta = info.get("metadata", {})
        title = meta.get(MetaKey.TITLE) or h.metadata.get("title") or ""
        parent_id = meta.get(MetaKey.PARENT_CHUNK_ID)
        if parent_id:
            parent_text = await get_segment_text_by_chunk_id(parent_id)
            if parent_text and len(parent_text) > max_parent:
                # 父分段过长（历史数据/超大章节）：改用命中的子分段原文，
                # 不按父段去重，否则同一大章节的多个命中会被压缩成一条巨块。
                chunks.append(RetrievedChunk(
                    chunk_id=h.chunk_id, text=h.text[:max_parent], score=h.score,
                    document_id=h.document_id, doc_version=h.doc_version,
                    title=title, source=h.source, similarity=h.vector_score,
                    metadata={**meta, MetaKey.CHUNK_ID: h.chunk_id,
                              MetaKey.PARENT_CHUNK_ID: parent_id}))
                continue
            if parent_text:
                # 命中子分段 → 用父分段全文替换，同一父分段只保留一条（去重）
                if parent_id in seen_parents:
                    continue
                seen_parents.add(parent_id)
                chunks.append(RetrievedChunk(
                    chunk_id=parent_id, text=parent_text, score=h.score,
                    document_id=h.document_id, doc_version=h.doc_version,
                    title=title, source=h.source, similarity=h.vector_score,
                    metadata={**meta, MetaKey.CHUNK_ID: parent_id}))
                continue
            # 父分段缺失（异常数据）：退化用子分段，不丢弃
            logger.warning("parent_chunk_missing", parent_id=parent_id, child=h.chunk_id)
        chunks.append(RetrievedChunk(
            chunk_id=h.chunk_id, text=h.text, score=h.score, document_id=h.document_id,
            doc_version=h.doc_version, title=title, source=h.source,
            similarity=h.vector_score, metadata=meta))

    # 3. 邻接兜底：短分段补同文档前后段（标题与正文被切开时仍能得到完整上下文）
    chunks = await _expand_short_neighbors(chunks)

    logger.info("retrieve_done", query=query_text[:60], hits=len(hits),
                chunks=len(chunks), vector=query_vector is not None,
                sources={s: sum(1 for h in hits if h.source == s)
                         for s in ("vector", "fulltext", "hybrid")})
    return chunks


async def aggregate(query_text: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """精排聚合：Rerank API 优先，失败降级 RRF；截断到 reranked_top_k 并过滤低分。"""
    cfg = get_settings().vector
    if not chunks:
        return []

    pairs = await get_rerank().rerank(query_text, [c.text for c in chunks],
                                      top_n=cfg.reranked_top_k)
    if pairs is not None:
        scores = [s for _, s in pairs]
        # 分数语义判别：全部落在 0~1 视为已归一化的相关性概率（阿里/Jina/vLLM），
        # 直接使用；出现负数或 >1 才按 Cross-Encoder logits 做 sigmoid。
        as_probability = bool(scores) and all(0.0 <= s <= 1.0 for s in scores)
        probs = ([(s, idx, s) for idx, s in pairs] if as_probability
                 else [(_sigmoid(s), idx, s) for idx, s in pairs])
        top_prob = max(p for p, _, _ in probs) if probs else 0.0
        floor = _PROB_TOP1_FLOOR if as_probability else _SIG_TOP1_FLOOR
        if top_prob < floor:
            logger.info("rerank_no_relevant", candidates=len(chunks),
                        top_prob=round(top_prob, 4))
            return []
        if as_probability:
            keep_above = max(top_prob - _PROB_REL_MARGIN, _PROB_MIN_KEEP)
        else:
            keep_above = top_prob - _SIG_REL_MARGIN
        ranked: list[RetrievedChunk] = []
        dropped = 0
        for prob, idx, score in probs:
            if prob < keep_above:
                dropped += 1
                continue
            c = chunks[idx]
            c.rerank_score = score
            if c.similarity is None:
                c.similarity = round(prob, 4)
            ranked.append(c)
        # 概率降序（原始分数同序，这里显式排序防上游乱序）
        ranked.sort(key=lambda c: -(c.rerank_score or 0.0))
        logger.info("rerank_done", pairs=len(pairs), kept=len(ranked), dropped=dropped,
                    top_prob=round(top_prob, 4), keep_above=round(keep_above, 4),
                    as_probability=as_probability, min_logit=cfg.min_score)
        return ranked[:cfg.reranked_top_k]

    # 无精排可用（本地部署常见：未配 Rerank 模型）：用问题词命中率做相关性校验。
    # 降级向量/BM25 排序并不能区分相关与无关，不过滤就会把“对不上”的片段当引用展示；
    # 宁可返回空结果，让回答提示“未检索到相关资料”，也不给误导性引用。
    terms = query_terms(query_text)
    scored = [(term_overlap(terms, c.text[:2000]), c) for c in chunks]
    # 两道门槛：核心词命中率 ≥ 0.4，且至少有一段 ≥3 字的连续原文匹配。
    # 实测"倒车入库的操作流程是什么"仅命中"操作/流程"的无关片段能到 0.36，
    # 单靠比例仍会漏过，加连续匹配后这类片段会被正确判为不相关。
    relevant = [(ov, c) for ov, c in scored
                if ov >= _MIN_TERM_OVERLAP and has_anchor(query_text, c.text[:2000])]
    if not relevant:
        logger.info("retrieval_no_relevant", candidates=len(chunks))
        return []
    relevant.sort(key=lambda p: (-p[0], -p[1].score))
    deduped: list[RetrievedChunk] = []
    seen: set[str] = set()
    for ov, c in relevant:
        if c.chunk_id in seen:
            continue
        seen.add(c.chunk_id)
        if c.similarity is None:
            c.similarity = round(ov, 4)
        deduped.append(c)
    return deduped[:cfg.reranked_top_k]


def build_references(chunks: list[RetrievedChunk]) -> list[RagReference]:
    """把最终进入 Prompt 的分段转为引用溯源（文档维度去重由前端处理）。

    similarity_score 取真实语义相似度/精排分，而不是内部 RRF 名次分（约 0.01~0.02，
    前端直接展示成“相似度”会让人误以为检索质量极差）。
    """
    refs: list[RagReference] = []
    for c in chunks:
        sim = c.similarity if c.similarity is not None else c.score
        refs.append(RagReference(
            document_id=str(c.document_id),
            document_title=c.title or None,
            chunk_id=c.chunk_id,
            version=str(c.doc_version) if c.doc_version else None,
            chunk_content=c.text[:500],
            similarity_score=round(sim, 4),
            retrieval_source=c.source,
            rerank_score=round(c.rerank_score, 4) if c.rerank_score is not None else None,
        ))
    return refs


async def retrieve_and_aggregate(query_text: str, *, user_identity: str | None = None,
                                 doc_versions: list[int] | None = None) -> tuple[list[RetrievedChunk], list[RagReference]]:
    """检索 + 聚合一步到位（知识库路由使用）。"""
    chunks = await retrieve(query_text, user_identity=user_identity, doc_versions=doc_versions)
    final = await aggregate(query_text, chunks)
    return final, build_references(final)
