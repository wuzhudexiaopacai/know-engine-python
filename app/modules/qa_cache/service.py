"""审核式问答缓存 Service。

状态机：PENDING →(审核) APPROVED →(激活/索引) ACTIVE →(禁用/过期) DISABLED/EXPIRED。
命中策略：对"标准问题"向量化，检索时与 ACTIVE 条目做余弦相似度，
    ≥ min_score（默认 0.95，近乎同义才命中）直接返回缓存答案，跳过整条 RAG，
    显著降低高频问题的时延与 Token 成本。

工程细节：
- 命中/未命中都写 qa_cache_lookup（低命中率准出统计的分母）
- 命中条目写回消息 metadata.entryId，负反馈可据此精准禁用
- 向量存 qa_cache_entry.embedding（LargeBinary），进程内 numpy 余弦：
    QA 缓存量级远小于文档分段，无需独立 ES 索引，且天然兼容 ES/降级两种部署形态
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select, update

from app.config.settings import get_settings
from app.core.database import session_scope
from app.core.exceptions import NotFoundError, ParamError
from app.core.logging import get_logger
from app.models.constants import CacheStatus, DisableReason
from app.models.entities import QaCacheEntry, QaCacheLookup
from app.models.queries import base_count, base_filter

logger = get_logger("app.qa_cache")


def _normalize(question: str) -> str:
    return "".join(question.lower().split())


def _question_key(question: str) -> str:
    return hashlib.md5(_normalize(question).encode()).hexdigest()


def _fingerprint(question: str) -> str:
    return hashlib.sha256(_normalize(question).encode()).hexdigest()[:32]


# ------------------------------------------------------------------
# 命中查询
# ------------------------------------------------------------------
async def lookup(question: str, conversation_id: str, assistant_msg_id: str) -> dict | None:
    """命中返回 {answer, references, entry_id}，未命中返回 None。整个过程只读 + 计数。"""
    cfg = get_settings().qa_cache
    if not cfg.enabled:
        return None
    from app.services.embedding import get_embedding
    try:
        q_vec = await get_embedding().embed_one(question)
    except Exception as e:
        logger.warning("qa_cache_embed_failed", error=str(e))
        return None

    async with session_scope() as session:
        rows = (await session.execute(
            select(QaCacheEntry).where(
                QaCacheEntry.status == CacheStatus.ACTIVE,
                QaCacheEntry.embedding.is_not(None)))).scalars().all()

    best, best_score = None, 0.0
    q = q_vec / (np.linalg.norm(q_vec) + 1e-9)
    for e in rows:
        v = np.frombuffer(e.embedding, dtype=np.float32)
        if v.size != q_vec.size:
            continue
        score = float(v / (np.linalg.norm(v) + 1e-9) @ q)
        if score > best_score:
            best, best_score = e, score

    if best is None or best_score < cfg.min_score:
        await _record_lookup(None, hit=False)   # 未命中也计数（命中率分母）
        return None

    # 命中：计数 + 回写消息标记（供负反馈精准禁用）
    async with session_scope() as session:
        await session.execute(update(QaCacheEntry)
                              .where(QaCacheEntry.id == best.id)
                              .values(hits=best.hits + 1))
        from app.models.entities import ChatMessage
        await session.execute(update(ChatMessage).where(ChatMessage.message_id == assistant_msg_id)
                              .values(cache_hit=1, metadata_={"qaCacheEntryId": best.id}))
    await _record_lookup(best.id, hit=True)
    logger.info("qa_cache_hit", entry_id=best.id, score=round(best_score, 4))
    return {"answer": best.answer, "references": _refs_from_entry(best), "entry_id": best.id}


async def _record_lookup(entry_id: str | None, hit: bool) -> None:
    async with session_scope() as session:
        session.add(QaCacheLookup(entry_id=entry_id, hit=hit))


def _refs_from_entry(entry: QaCacheEntry) -> list[dict]:
    """缓存条目命中时的溯源：复用挖掘时保存的来源问答快照。"""
    srcs = entry.sources or []
    refs = []
    for s in srcs:
        if isinstance(s, dict):
            refs.append({
                "documentId": str(s.get("documentId", "")),
                "documentTitle": s.get("documentTitle"),
                "chunkId": s.get("chunkId"),
                "version": s.get("version"),
                "chunkContent": (s.get("content") or s.get("answer") or "")[:500],
                "similarityScore": None,
                "retrievalSource": "qa_cache", "rerankScore": None})
    return refs


# ------------------------------------------------------------------
# 挖掘：高频问答 → PENDING 候选
# ------------------------------------------------------------------
async def record_qa(question: str, answer: str, sources: list[dict] | None = None) -> None:
    """对话成功后调用：命中频次阈值则沉淀为待审条目（默认关闭，QA_CACHE__ENABLED=true 启用）。

    sources 为检索到的分段快照（documentId/chunkId/content…），命中缓存时
    据此还原可定位的引用。
    """
    cfg = get_settings().qa_cache
    if not cfg.enabled or not answer or len(answer) < 10:
        return
    key = _question_key(question)
    # 频次用 L2 计数（Redis 可用则跨进程共享，否则进程内）
    from app.infra.cache import get_cache
    cache = get_cache()
    ck = f"ke:qa:freq:{key}"
    count_raw = await cache.get_raw(ck)
    count = (int(count_raw) if count_raw else 0) + 1
    await cache.set_raw(ck, count, ttl=7 * 24 * 3600)
    if count < cfg.min_frequency:
        return

    fp = _fingerprint(question)
    async with session_scope() as session:
        exists = (await session.execute(
            select(QaCacheEntry).where(QaCacheEntry.fingerprint == fp))).scalar_one_or_none()
        if exists is not None:
            if exists.status in (CacheStatus.REJECTED, CacheStatus.DISABLED):
                return  # 被人工否决过的不再自动重挖
            await session.execute(update(QaCacheEntry).where(QaCacheEntry.id == exists.id)
                                  .values(frequency=count))
            return
        entry = QaCacheEntry(
            id=_new_id(), fingerprint=fp, question_key=key, question=question[:500],
            answer=answer,
            sources=[s for s in (sources or []) if s.get("documentId")],
            frequency=count, status=CacheStatus.PENDING)
        session.add(entry)
    logger.info("qa_cache_candidate_mined", question=question[:40], freq=count)


def _new_id() -> str:
    from app.infra.snowflake import next_id_str
    return next_id_str()


# ------------------------------------------------------------------
# 审核工作流
# ------------------------------------------------------------------
async def list_entries(status: str | None, page: int, size: int) -> dict:
    async with session_scope() as session:
        stmt = base_filter(QaCacheEntry)
        cnt = base_count(QaCacheEntry)
        if status:
            stmt = stmt.where(QaCacheEntry.status == status)
            cnt = cnt.where(QaCacheEntry.status == status)
        total = (await session.execute(cnt)).scalar_one()
        rows = (await session.execute(
            stmt.order_by(QaCacheEntry.created_at.desc())
            .offset((page - 1) * size).limit(size))).scalars().all()
        records = [_entry_to_dict(e) for e in rows]
    return {"total": total, "page_num": page, "page_size": size, "records": records}


async def approve(entry_id: str, reviewer: str, note: str | None) -> dict:
    """通过审核：PENDING →(规范化) APPROVED →(建立向量索引) ACTIVE。

    索引失败时停留在 APPROVED（附 index_error），由补偿任务重试后再激活，
    避免"已上线但检索不到"的假生效状态。
    """
    async with session_scope() as session:
        entry = (await session.execute(
            select(QaCacheEntry).where(QaCacheEntry.id == entry_id))).scalar_one_or_none()
        if entry is None:
            raise NotFoundError("缓存条目不存在")
        if entry.status not in (CacheStatus.PENDING, CacheStatus.APPROVED):
            raise ParamError(f"当前状态不可审核: {entry.status}")
        from_status = entry.status

    if from_status == CacheStatus.PENDING:
        curated = await _curate_answer(entry_id)
        if curated is not None and curated.get("reject"):
            raise ParamError(f"该问答不适合公开复用，请驳回：{curated.get('reason') or '涉及个人信息或实时环境'}")

    now = datetime.now()
    ttl_days = get_settings().qa_cache.ttl_days
    expires_at = now + timedelta(days=ttl_days) if ttl_days > 0 else None
    async with session_scope() as session:
        await session.execute(update(QaCacheEntry).where(QaCacheEntry.id == entry_id)
                              .values(status=CacheStatus.APPROVED, reviewer=reviewer,
                                      review_note=note, reviewed_at=now,
                                      expires_at=expires_at, index_error=None))
    await _index_embedding(entry_id, activate=True)

    async with session_scope() as session:
        final_status = (await session.execute(
            select(QaCacheEntry.status).where(QaCacheEntry.id == entry_id))).scalar_one()
    return {"id": entry_id, "status": final_status}


async def reject(entry_id: str, reviewer: str, note: str | None) -> dict:
    async with session_scope() as session:
        r = await session.execute(update(QaCacheEntry).where(QaCacheEntry.id == entry_id)
                                  .values(status=CacheStatus.REJECTED, reviewer=reviewer,
                                          review_note=note, reviewed_at=datetime.now()))
        if r.rowcount == 0:
            raise NotFoundError("缓存条目不存在")
    return {"id": entry_id, "status": CacheStatus.REJECTED}


async def disable(entry_id: str, reason: str, detail: str | None) -> dict:
    """人工/自动准出：置 DISABLED，清向量，后续不再命中。"""
    async with session_scope() as session:
        await session.execute(update(QaCacheEntry).where(QaCacheEntry.id == entry_id)
                              .values(status=CacheStatus.DISABLED, disable_reason=reason,
                                      disable_detail=detail, disabled_at=datetime.now(),
                                      embedding=None, vector_deleted=1))
    return {"id": entry_id, "status": CacheStatus.DISABLED}


async def invalidate_by_document(doc_id: int, detail: str | None = None) -> int:
    """来源文档被删除时，失效引用该文档的 QA 缓存条目。

    匹配依据：entry.sources 内每个来源快照的 documentId。命中条目置 DISABLED、
    清向量（embedding=None、vector_deleted=1），并删其 Redis 频次计数，避免
    陈旧答案继续命中或被重新挖掘。QA 缓存量级小，在 Python 侧过滤 sources 以兼容
    MySQL 与降级 SQLite（避免方言相关的 JSON 函数）。返回失效条目数。
    """
    async with session_scope() as session:
        rows = (await session.execute(
            select(QaCacheEntry).where(
                QaCacheEntry.status.in_(
                    [CacheStatus.PENDING, CacheStatus.APPROVED, CacheStatus.ACTIVE]))
        )).scalars().all()
        target = [e for e in rows if _entry_references_doc(e.sources, doc_id)]
        for e in target:
            e.status = CacheStatus.DISABLED
            e.disable_reason = DisableReason.KNOWLEDGE_CHANGED
            e.disable_detail = detail or f"来源文档 {doc_id} 已删除"
            e.disabled_at = datetime.now()
            e.embedding = None
            e.vector_deleted = 1
            session.add(e)
    # 同步清该条目的 Redis 频次计数（L1+L2），expire_on_commit=False 下 question_key 仍可读
    from app.infra.cache import get_cache
    cache = get_cache()
    for e in target:
        if e.question_key:
            await cache.delete(f"ke:qa:freq:{e.question_key}")
    if target:
        logger.info("qa_cache_invalidated_by_document", doc_id=doc_id, count=len(target))
    return len(target)


def _entry_references_doc(sources: list | None, doc_id: int) -> bool:
    """判断来源快照是否引用指定文档（documentId 可能存 int 或 str，两者都兼容）。"""
    if not sources:
        return False
    want = str(doc_id)
    for s in sources:
        if isinstance(s, dict):
            d = s.get("documentId")
            if d is not None and str(d) == want:
                return True
    return False


async def on_negative_feedback(message_id: str, comment: str | None) -> None:
    """点踩一条"缓存命中"的答案 → 精准禁用对应条目（负反馈自动准出）。"""
    async with session_scope() as session:
        from app.models.entities import ChatMessage
        msg = (await session.execute(
            select(ChatMessage).where(ChatMessage.message_id == message_id))).scalar_one_or_none()
        if msg is None or not msg.cache_hit:
            return
        entry_id = (msg.metadata_ or {}).get("qaCacheEntryId")
    if entry_id:
        await disable(entry_id, DisableReason.NEGATIVE_FEEDBACK, comment)
        logger.info("qa_cache_disabled_by_feedback", entry_id=entry_id)


async def _index_embedding(entry_id: str, *, activate: bool = False) -> None:
    """为标准问题生成向量并落库；activate=True 时 APPROVED → ACTIVE 正式生效。

    失败只记录 index_error，由补偿任务重试，不影响已审核结论。
    """
    from app.services.embedding import get_embedding
    async with session_scope() as session:
        entry = (await session.execute(
            select(QaCacheEntry).where(QaCacheEntry.id == entry_id))).scalar_one_or_none()
        if entry is None:
            return
        entry_status = entry.status
    try:
        vec = await get_embedding().embed_one(entry.question)
        values: dict = {"embedding": vec.tobytes(), "index_error": None}
        if activate and entry_status == CacheStatus.APPROVED:
            values["status"] = CacheStatus.ACTIVE
            values["activated_at"] = datetime.now()
        async with session_scope() as session:
            await session.execute(update(QaCacheEntry).where(QaCacheEntry.id == entry_id)
                                  .values(**values))
    except Exception as e:
        logger.warning("qa_cache_index_failed", entry_id=entry_id, error=str(e))
        async with session_scope() as session:
            await session.execute(update(QaCacheEntry).where(QaCacheEntry.id == entry_id)
                                  .values(index_error=str(e)[:256]))


def _parse_curate_json(raw: str) -> dict | None:
    """从 LLM 输出中取出 JSON（容忍 ```json 包裹与前后解释性文字）。"""
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def _curate_answer(entry_id: str) -> dict | None:
    """审核通过前用 LLM 规范化问答并落质量分。

    返回 {"reject": True, "reason": ...} 表示不可公开复用；返回 None 表示
    LLM 不可用或输出不可解析（保留原文，质量分不变）。
    """
    llm = get_llm_safe()
    if llm is None:
        return None
    async with session_scope() as session:
        entry = (await session.execute(
            select(QaCacheEntry).where(QaCacheEntry.id == entry_id))).scalar_one_or_none()
        if entry is None:
            return None
        question, answer = entry.question, entry.answer
    evidence = json.dumps([{"question": question, "answer": answer}], ensure_ascii=False)
    try:
        from app.services.prompt_loader import render
        raw = await llm.chat(
            render("qa-cache-curate-prompt", evidence=evidence),
            model=llm.flash_model, temperature=0.2)
        data = _parse_curate_json(raw or "")
        if data is None:
            logger.warning("qa_cache_curate_parse_failed", entry_id=entry_id)
            return None
        if data.get("reject"):
            return {"reject": True, "reason": str(data.get("reason") or "")[:1000]}

        std_question = str(data.get("standard_question") or question).strip()[:500] or question
        std_answer = str(data.get("answer") or answer).strip() or answer
        try:
            quality = float(data.get("quality") or 0.0)
        except (TypeError, ValueError):
            quality = 0.0
        quality = max(0.0, min(1.0, quality))
        reason = str(data.get("reason") or "")[:1000] or None
        async with session_scope() as session:
            await session.execute(update(QaCacheEntry).where(QaCacheEntry.id == entry_id)
                                  .values(question=std_question, answer=std_answer,
                                          quality=quality, reason=reason))
        logger.info("qa_cache_curated", entry_id=entry_id, quality=round(quality, 4))
        return {"reject": False, "quality": quality}
    except Exception as e:
        logger.warning("qa_cache_curate_failed", error=str(e))
        return None


def get_llm_safe():
    from app.services.llm_client import get_llm
    llm = get_llm()
    return llm if llm.available else None


def _entry_to_dict(e: QaCacheEntry) -> dict:
    return {
        "id": e.id, "question": e.question, "answer": e.answer,
        "status": e.status, "frequency": e.frequency, "hits": e.hits,
        "quality": e.quality, "reviewer": e.reviewer, "review_note": e.review_note,
        "disable_reason": e.disable_reason,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "activated_at": e.activated_at.isoformat() if e.activated_at else None,
    }
