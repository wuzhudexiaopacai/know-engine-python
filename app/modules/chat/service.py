"""对话与会话管理 Service。

历史记忆：滑动窗口取最近 N 条消息拼成
"role: content" 文本注入改写/意图/生成 Prompt，超窗口自动截断，控制 Token 成本。
"""

from __future__ import annotations

from sqlalchemy import func, select, update

from app.core.database import session_scope
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.infra.snowflake import next_id_str
from app.models.constants import ConversationStatus, MessageType
from app.models.entities import (
    ChatConversation, ChatConversationSummary, ChatMessage, UserPreference)
from app.models.queries import base_filter

logger = get_logger("app.chat.service")

_MEMORY_WINDOW = 10   # 滑动窗口默认大小：最近 10 条消息（5 轮）


# ------------------------------------------------------------------
# 会话
# ------------------------------------------------------------------
async def ensure_conversation(owner_key: str, conversation_id: str | None,
                              first_question: str) -> tuple[str, bool]:
    """返回 (conversation_id, is_new)。无 conversation_id 则用问题前 20 字建临时标题，
    异步标题生成完成后再回写覆盖（见 pipeline）。

    传入的 conversation_id 若不属于当前用户，不回传也不复用，直接新建会话，
    避免通过猜测 conversation_id 写入他人会话。
    """
    if conversation_id:
        async with session_scope() as session:
            exists = (await session.execute(
                base_filter(ChatConversation).where(
                    ChatConversation.conversation_id == conversation_id))).scalar_one_or_none()
        if exists is not None and exists.user_id == owner_key:
            return conversation_id, False
    conv_id = next_id_str()
    temp_title = first_question[:20]
    async with session_scope() as session:
        session.add(ChatConversation(conversation_id=conv_id, user_id=owner_key,
                                     title=temp_title, status=ConversationStatus.ACTIVE))
    return conv_id, True


async def update_title(conversation_id: str, title: str) -> None:
    async with session_scope() as session:
        await session.execute(update(ChatConversation)
                              .where(ChatConversation.conversation_id == conversation_id)
                              .values(title=title[:512]))


async def list_conversations(owner_key: str, include_all: bool = False) -> list[dict]:
    """会话列表：默认仅本人的会话；管理员可传 include_all 查看全部（带归属标识）。"""
    async with session_scope() as session:
        stmt = base_filter(ChatConversation)
        if not include_all:
            stmt = stmt.where(ChatConversation.user_id == owner_key)
        # 按“最近活动”倒序：优先取最后一条消息时间，无消息回退创建时间，
        # 保证前端刷新后自动选中的会话就是用户上次聊过的那个
        last_msg_at = (
            select(func.max(ChatMessage.created_at))
            .where(ChatMessage.conversation_id == ChatConversation.conversation_id)
            .correlate(ChatConversation)
            .scalar_subquery()
        )
        rows = (await session.execute(
            stmt.order_by(func.coalesce(last_msg_at, ChatConversation.created_at).desc())
            .limit(200))).scalars().all()
    return [{"conversation_id": c.conversation_id, "title": c.title,
             "status": c.status, "created_at": c.created_at.isoformat(),
             "owner": c.user_id} for c in rows]


async def _require_conversation(session, conversation_id: str, owner_key: str,
                                is_admin: bool) -> ChatConversation:
    """归属校验：非本人且非管理员一律按"不存在"处理，不泄露会话是否真实存在。"""
    conv = (await session.execute(
        base_filter(ChatConversation).where(
            ChatConversation.conversation_id == conversation_id))).scalar_one_or_none()
    if conv is None:
        raise NotFoundError("会话不存在")
    if not is_admin and conv.user_id != owner_key:
        raise NotFoundError("会话不存在")
    return conv


async def verify_conversation_access(conversation_id: str, owner_key: str,
                                     is_admin: bool) -> None:
    async with session_scope() as session:
        await _require_conversation(session, conversation_id, owner_key, is_admin)


async def delete_conversation(conversation_id: str, owner_key: str,
                              is_admin: bool = False) -> None:
    from app.models.queries import soft_delete
    async with session_scope() as session:
        await _require_conversation(session, conversation_id, owner_key, is_admin)
        await soft_delete(session, ChatConversation, "conversation_id", conversation_id)


# ------------------------------------------------------------------
# 消息
# ------------------------------------------------------------------
async def save_user_message(conversation_id: str, content: str) -> str:
    message_id = next_id_str()
    async with session_scope() as session:
        session.add(ChatMessage(message_id=message_id, conversation_id=conversation_id,
                                type=MessageType.USER, content=content))
    return message_id


async def save_assistant_message(conversation_id: str) -> str:
    """先落一条空的助手消息占位（拿到 message_id 供评价按钮绑定），流结束后回填内容。"""
    message_id = next_id_str()
    async with session_scope() as session:
        session.add(ChatMessage(message_id=message_id, conversation_id=conversation_id,
                                type=MessageType.ASSISTANT, content=""))
    return message_id


async def update_assistant_content(message_id: str, content: str, *,
                                  transform_content: str | None = None,
                                  model_name: str | None = None,
                                  cache_hit: int = 0) -> None:
    values: dict = {"content": content, "token_count": len(content)}
    if transform_content is not None:
        values["transform_content"] = transform_content
    if model_name is not None:
        values["model_name"] = model_name
    if cache_hit:
        values["cache_hit"] = 1
    async with session_scope() as session:
        await session.execute(update(ChatMessage)
                              .where(ChatMessage.message_id == message_id).values(**values))


async def update_rag_references(message_id: str, references: list[dict]) -> None:
    async with session_scope() as session:
        await session.execute(update(ChatMessage)
                              .where(ChatMessage.message_id == message_id)
                              .values(rag_references=references))


async def reference_detail(chunk_id: str) -> dict:
    """引用片段预览：返回文档、章节路径、段序与全文，供前端定位查看。

    chunk_id 不存在的按“引用已失效”处理，不泄露其他信息。
    """
    from app.models.constants import MetaKey
    from app.models.entities import DocumentVersion, KnowledgeDocument, KnowledgeSegment
    async with session_scope() as session:
        seg = (await session.execute(
            base_filter(KnowledgeSegment).where(
                KnowledgeSegment.chunk_id == chunk_id))).scalar_one_or_none()
        if seg is None:
            raise NotFoundError("引用片段不存在或已失效")
        doc = (await session.execute(
            base_filter(KnowledgeDocument).where(
                KnowledgeDocument.doc_id == seg.document_id))).scalar_one_or_none()
        if doc is None:
            raise NotFoundError("引用所在文档已被删除")
        ver = (await session.execute(
            base_filter(DocumentVersion).where(
                DocumentVersion.version_id == seg.document_version))).scalar_one_or_none()
        total = (await session.execute(
            select(func.count()).select_from(KnowledgeSegment).where(
                KnowledgeSegment.document_id == seg.document_id,
                KnowledgeSegment.document_version == seg.document_version,
                KnowledgeSegment.deleted == 0))).scalar_one()
    meta = seg.metadata_ or {}
    text = seg.text or ""
    return {
        "documentId": str(seg.document_id),
        "documentTitle": doc.doc_title,
        "version": ver.version if ver is not None else None,
        "chunkId": seg.chunk_id,
        "sectionPath": meta.get(MetaKey.TITLE) or "",
        "order": seg.chunk_order + 1,
        "total": int(total),
        "content": text[:4000],
        "truncated": len(text) > 4000,
    }


async def get_history(conversation_id: str, user_id: str | None = None) -> str:
    """滑动窗口历史（供 Prompt 上下文）。

    若提供 user_id：从用户偏好取窗口大小；长期记忆开启时在历史前注入最新摘要。
    返回单字符串：[# 对话摘要\\n{summary}\\n\\n# 最近对话\\n] + 最近 N 条消息文本。
    偏好/摘要查询失败时降级为默认窗口、无摘要，不影响主流程。
    """
    window = _MEMORY_WINDOW
    summary_text = ""
    if user_id:
        try:
            pref = await get_preference(user_id)
            window = max(2, min(100, int(pref.memory_window_size)))
            if pref.long_term_memory_enabled:
                s = await get_summary(conversation_id)
                if s and s.summary_text:
                    summary_text = s.summary_text
        except Exception as e:
            logger.warning("get_history_pref_failed", error=str(e))

    async with session_scope() as session:
        rows = (await session.execute(
            base_filter(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id,
                   ChatMessage.content.is_not(None))
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(window))).scalars().all()
    lines = []
    for m in reversed(rows):
        role = "用户" if m.type == MessageType.USER else "助手"
        if m.content:
            lines.append(f"{role}: {m.content[:500]}")
    history = "\n".join(lines)
    if summary_text:
        return f"# 对话摘要\n{summary_text}\n\n# 最近对话\n{history}"
    return history


async def count_messages_since_summary(conversation_id: str) -> int:
    """自上次摘要以来尚未折叠的消息条数（用于判断是否触发新一轮摘要）。

    总消息数 - 已折叠进摘要的消息数 = 未折叠消息数。
    """
    s = await get_summary(conversation_id)
    folded = s.message_count if s else 0
    async with session_scope() as session:
        total = (await session.execute(
            select(func.count()).select_from(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id,
                   ChatMessage.deleted == 0))).scalar_one()
    return max(0, int(total) - folded)


# ------------------------------------------------------------------
# 用户偏好
# ------------------------------------------------------------------
async def get_preference(user_id: str) -> UserPreference:
    """获取用户对话偏好，无记录则创建默认行并返回。"""
    async with session_scope() as session:
        pref = (await session.execute(
            select(UserPreference).where(UserPreference.user_id == user_id)
        )).scalar_one_or_none()
        if pref is None:
            pref = UserPreference(user_id=user_id)
            session.add(pref)
            await session.flush()
            await session.refresh(pref)
        return pref


async def update_preference(user_id: str, **fields) -> UserPreference:
    """更新用户对话偏好，仅允许白名单字段；无记录则按给定值创建。"""
    allowed = {"long_term_memory_enabled", "memory_window_size", "summary_round_interval"}
    values = {k: v for k, v in fields.items() if k in allowed}
    async with session_scope() as session:
        pref = (await session.execute(
            select(UserPreference).where(UserPreference.user_id == user_id)
        )).scalar_one_or_none()
        if pref is None:
            pref = UserPreference(user_id=user_id, **values)
            session.add(pref)
        else:
            for k, v in values.items():
                setattr(pref, k, v)
        await session.flush()
        await session.refresh(pref)
        return pref


def preference_to_dict(p: UserPreference) -> dict:
    return {
        "long_term_memory_enabled": p.long_term_memory_enabled,
        "memory_window_size": p.memory_window_size,
        "summary_round_interval": p.summary_round_interval,
    }


# ------------------------------------------------------------------
# 长期摘要
# ------------------------------------------------------------------
async def get_summary(conversation_id: str) -> ChatConversationSummary | None:
    """返回会话最新摘要（若无则 None）。"""
    async with session_scope() as session:
        return (await session.execute(
            base_filter(ChatConversationSummary)
            .where(ChatConversationSummary.conversation_id == conversation_id)
            .order_by(ChatConversationSummary.id.desc())
            .limit(1))).scalar_one_or_none()


async def save_summary(conversation_id: str, text: str, message_count: int) -> None:
    """保存新摘要（追加一条新记录，保留历史便于回溯）。"""
    async with session_scope() as session:
        session.add(ChatConversationSummary(
            conversation_id=conversation_id,
            summary_text=text[:4000],
            token_count=len(text),
            message_count=message_count,
        ))


async def recent_memory_topics(owner_key: str, limit: int = 8) -> list[str]:
    """返回用户近期会话的记忆素材：优先长期摘要，无摘要时回退会话标题。

    供新会话「你可能感兴趣」建议使用，让推荐真正受长期记忆影响。
    """
    async with session_scope() as session:
        last_msg_at = (
            select(func.max(ChatMessage.created_at))
            .where(ChatMessage.conversation_id == ChatConversation.conversation_id)
            .correlate(ChatConversation)
            .scalar_subquery()
        )
        convs = (await session.execute(
            base_filter(ChatConversation)
            .where(ChatConversation.user_id == owner_key)
            .order_by(func.coalesce(last_msg_at, ChatConversation.created_at).desc())
            .limit(limit))).scalars().all()
        conv_ids = [c.conversation_id for c in convs]
        if not conv_ids:
            return []
        rows = (await session.execute(
            base_filter(ChatConversationSummary)
            .where(ChatConversationSummary.conversation_id.in_(conv_ids))
            .order_by(ChatConversationSummary.id.desc()))).scalars().all()
    latest: dict[str, str] = {}
    for s in rows:
        if s.summary_text:
            latest.setdefault(s.conversation_id, s.summary_text)
    topics: list[str] = []
    for c in convs:
        text = latest.get(c.conversation_id) or c.title
        if text:
            topics.append(text[:500])
    return topics


async def list_messages(conversation_id: str, owner_key: str,
                        is_admin: bool = False) -> list[dict]:
    async with session_scope() as session:
        await _require_conversation(session, conversation_id, owner_key, is_admin)
        rows = (await session.execute(
            base_filter(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc()))).scalars().all()
    return [_message_to_dict(m) for m in rows]


async def feedback(message_id: str, helpful: bool, comment: str | None,
                   owner_key: str, is_admin: bool = False) -> None:
    """答案点赞/点踩：helpful 1/0 + 意见；负反馈将触发 QA 缓存条目准出（见 qa_cache）。"""
    from datetime import datetime
    async with session_scope() as session:
        msg = (await session.execute(
            select(ChatMessage).where(ChatMessage.message_id == message_id))).scalar_one_or_none()
        if msg is None:
            raise NotFoundError("消息不存在")
        await _require_conversation(session, msg.conversation_id, owner_key, is_admin)
        await session.execute(update(ChatMessage)
                              .where(ChatMessage.message_id == message_id)
                              .values(helpful=1 if helpful else 0,
                                      feedback_comment=comment, feedback_at=datetime.now()))
    if not helpful:
        from app.modules.qa_cache import service as qa_cache
        await qa_cache.on_negative_feedback(message_id, comment)


def _message_to_dict(m: ChatMessage) -> dict:
    return {
        "message_id": m.message_id,
        "type": m.type,
        "content": m.content,
        "transform_content": m.transform_content,
        "model_name": m.model_name,
        "rag_references": m.rag_references or [],
        "cache_hit": m.cache_hit,
        "helpful": m.helpful,
        "created_at": m.created_at.isoformat(),
    }
