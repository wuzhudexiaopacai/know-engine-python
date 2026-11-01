"""RAG 流式对话编排。

以异步生成器形式串起全管道，逐阶段产出 (event, data) 供 controller 编码为 SSE：

    QA缓存命中? ──是──> 直接吐出缓存答案(+引用) → done
        │否
     意图识别 → 不相关 → 通用对话直答
        │相关
     查询改写 → 查询路由 → {知识库 | Text2SQL | Text2Cypher} 检索 → 精排聚合
        → 引用溯源 → 流式生成（含卡片） → 回填消息 → done

每个外部环节都自带降级（见各子模块），pipeline 只做编排与异常兜底，
保证"任何一环失败都能给用户一个可解释的回复"，而非 500。
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
import time
from typing import AsyncIterator

from app.config.settings import get_settings
from app.core.exceptions import ParamError
from app.core.logging import get_logger
from app.infra import stop_signal
from app.modules.chat import service as chat
from app.modules.document import service as document_service
from app.modules.model import service as model_service
from app.modules.rag import analysis, graph, retrieval, text2sql
from app.modules.rag.model import IntentResult, RetrievedChunk, RouteResult
from app.services.llm_client import get_llm, get_model_llm
from app.services.prompt_loader import render

logger = get_logger("app.rag.pipeline")

# 结构化数据查询需要感知可用表 schema（供路由 Prompt）
_TABLE_HINT = "tech_article(技术文章：语言/框架/分类/阅读量/点赞), knowledge_subscription(订阅：主题/难度/频率/状态)"

_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_IMAGE_SIDE = 1024          # 视觉模型上下文有限：大图按最长边缩放，避免图片 token 撑爆上下文


def _shrink_image(data: bytes, max_side: int = _MAX_IMAGE_SIDE) -> tuple[bytes, str | None]:
    """按最长边等比缩放并输出 JPEG；失败时原样返回（不影响主流程）。"""
    try:
        import io
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img.load()
        w, h = img.size
        longest = max(w, h)
        if longest > max_side:
            scale = max_side / longest
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale)),
                              ), Image.Resampling.LANCZOS)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        logger.warning("chat_image_shrink_failed", error=str(e))
        return data, None


async def _load_image_parts(urls: list[str]) -> list[dict]:
    """把 /api/files/ 图片读取为视觉模型可用的 data URL 内容块（最多 4 张）。"""
    from app.infra.file_storage import get_file_store
    parts: list[dict] = []
    # 多图时缩小单图尺寸：本地视觉模型上下文有限，多图叠加同样会超限
    side = _MAX_IMAGE_SIDE if len(urls) <= 2 else 768
    for url in urls:
        if not url.startswith("/api/files/"):
            continue
        key = url[len("/api/files/"):]
        try:
            path = await get_file_store().get(key)
            data = await asyncio.to_thread(path.read_bytes)
        except Exception as e:
            logger.warning("chat_image_load_failed", key=key, error=str(e))
            continue
        if not data or len(data) > _MAX_IMAGE_BYTES:
            continue
        data, mime = _shrink_image(data, side)
        mime = mime or mimetypes.guess_type(key)[0] or "image/png"
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}})
    return parts


def _fmt_contexts(chunks: list[RetrievedChunk]) -> str:
    """把检索分段拼成带来源编号的参考资料块，注入回答 Prompt。

    单条与总量都做预算截断：历史数据里存在几万字的父分段，整篇注入会挤爆上下文，
    导致模型丢失问题本身、回答变差。
    """
    if not chunks:
        return "（未检索到相关资料）"
    cfg = get_settings().vector
    blocks: list[str] = []
    used = 0
    for i, c in enumerate(chunks, 1):
        text = c.text
        if len(text) > cfg.max_context_chars:
            text = text[:cfg.max_context_chars] + "\n（该段过长，已截断）"
        block = f"[资料{i}]" + (f" 《{c.title}》" if c.title else "") + f"\n{text}"
        if blocks and used + len(block) > cfg.max_prompt_context_chars:
            logger.info("context_budget_reached", kept=len(blocks), total=len(chunks))
            break
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks)


async def stream_chat(user_id: str, conversation_id: str | None, question: str,
                      *, user_identity: str | None = None,
                      use_cache: bool = True,
                      model_id: int | None = None,
                      images: list[str] | None = None) -> AsyncIterator[tuple[str, dict]]:
    """主入口：注册会话级停止信号后转发内部生成器。

    传 images 时改用「图片理解」视觉模型做多模态生成，并跳过意图/改写/路由；
    事件同时写入会话缓冲，刷新后可通过 /api/chat/stream/attach 续流。

    客户端断线（GeneratorExit）或正常结束时均注销信号，
    避免遗留置位事件误伤下一轮对话。
    """
    from app.infra import chat_stream as hub
    from app.models.constants import SseEvent

    runtime = await model_service.resolve_runtime(model_id)
    pics = [u for u in (images or []) if u][:4]
    image_parts = await _load_image_parts(pics)
    if image_parts:
        vision = await model_service.resolve_vision_runtime()
        if vision is None:
            raise ParamError("请先在「模型配置」中启用图片理解模型")
        llm = get_model_llm(vision.id, vision.lock_version, vision.base_url,
                            vision.api_key, vision.model_name, vision.allow_empty_key)
        model_name = vision.display_name or vision.model_name
        skip_analysis = True
    else:
        llm = get_model_llm(runtime.id, runtime.lock_version, runtime.base_url,
                            runtime.api_key, runtime.model_name, runtime.allow_empty_key,
                            runtime.supports_vision)
        model_name = runtime.display_name or runtime.model_name
        # 本地部署（CPU 推理）跑一次分析就要几十秒，跳过意图/改写/路由三连，直接检索生成
        skip_analysis = runtime.allow_empty_key
    display_question = question + "".join(f"\n\n![]({u})" for u in pics)
    conv_id, is_new = await chat.ensure_conversation(user_id, conversation_id, question)
    # 新会话首条消息后异步生成语义标题（不阻塞主回答流）
    if is_new:
        asyncio.create_task(_async_title(conv_id, question))
    stop_event = stop_signal.register(conv_id)
    hub.begin(conv_id)
    try:
        yield SseEvent.CONVERSATION, {"conversation_id": conv_id}
        async for item in _stream_chat(
                stop_event, user_id, conv_id, question,
                user_identity=user_identity, use_cache=use_cache and not image_parts,
                llm=llm, model_name=model_name, skip_analysis=skip_analysis,
                display_question=display_question, image_parts=image_parts):
            hub.publish(conv_id, *item)
            yield item
    finally:
        hub.finish(conv_id)
        stop_signal.unregister(conv_id)


async def _stream_chat(stop_event: asyncio.Event, user_id: str,
                       conv_id: str, question: str,
                       *, user_identity: str | None = None,
                       use_cache: bool = True, llm=None,
                       model_name: str | None = None,
                       skip_analysis: bool = False,
                       display_question: str | None = None,
                       image_parts: list[dict] | None = None) -> AsyncIterator[tuple[str, dict]]:
    """管道主体：产出 (event_name, data) 序列。event_name 取自 SseEvent。

    会话已由主入口 ensure 并注册停止信号，此处直接复用，不再重复建会话。"""
    from app.models.constants import SseEvent

    def progress(msg: str) -> tuple[str, dict]:
        return SseEvent.PROGRESS, {"message": msg}

    user_msg_id = await chat.save_user_message(conv_id, display_question or question)
    assistant_msg_id = await chat.save_assistant_message(conv_id)

    last_flush = 0.0

    async def flush_partial(text: str) -> None:
        """流式期间按 1s 节流回填答案：浏览器关闭、进程重启时已生成内容不丢失。"""
        nonlocal last_flush
        now = time.monotonic()
        if now - last_flush < 1.0:
            return
        last_flush = now
        try:
            await chat.update_assistant_content(assistant_msg_id, text)
        except Exception as e:      # 回填失败不影响主流程，最终还会整段覆盖
            logger.warning("assistant_flush_failed", error=str(e))

    # ---- 0. 图片理解：多模态直答，跳过 QA 缓存/意图/改写/路由/知识库检索 ----
    # 图片问题与知识库无关，走检索只会带出对不上的引用，且小模型易被长资料 prompt 拖垮。
    if image_parts:
        history = await chat.get_history(conv_id, user_id)
        yield progress("正在分析图片...")
        answer = ""
        async for token in _vision_chat(question, history, image_parts, llm=llm):
            answer += token
            yield SseEvent.TOKEN, {"text": token}
            await flush_partial(answer)
            if stop_event.is_set():
                break
        await chat.update_assistant_content(assistant_msg_id, answer, model_name=model_name)
        asyncio.create_task(_maybe_summarize(user_id, conv_id))
        yield SseEvent.ANSWER_MESSAGE, {"message_id": assistant_msg_id}
        _suggestion = await _generate_suggestions(question, answer, llm=llm)
        if _suggestion:
            yield _suggestion
        yield SseEvent.DONE, {"conversation_id": conv_id, "message_id": assistant_msg_id}
        return

    # ---- 1. QA 缓存命中：直接返回审核过的标准答案 ----
    if use_cache:
        try:
            from app.modules.qa_cache import service as qa_cache
            hit = await qa_cache.lookup(question, conv_id, assistant_msg_id)
        except Exception as e:
            logger.warning("qa_cache_lookup_failed", error=str(e))
            hit = None
        if hit is not None:
            # 缓存命中同样要落库：否则刷新后助手气泡只剩"命中缓存"标签、正文为空
            refs = hit.get("references") or []
            answer = hit["answer"]
            await chat.update_assistant_content(assistant_msg_id, answer, cache_hit=1)
            if refs:
                await chat.update_rag_references(assistant_msg_id, refs)
            yield progress("命中知识库问答缓存")
            yield SseEvent.TOKEN, {"text": answer}
            if refs:
                yield SseEvent.REFERENCE, {"references": refs}
            asyncio.create_task(_maybe_summarize(user_id, conv_id))
            yield SseEvent.ANSWER_MESSAGE, {"message_id": assistant_msg_id}
            _suggestion = await _generate_suggestions(question, answer, llm=llm)
            if _suggestion:
                yield _suggestion
            yield SseEvent.DONE, {"conversation_id": conv_id, "message_id": assistant_msg_id}
            return

    # ---- 2. 意图识别 ----
    history = await chat.get_history(conv_id, user_id)
    if skip_analysis:
        # 本地模型：三连分析太慢，直接按"相关+知识库"处理
        intent = IntentResult(related=True, intent="其他", reasoning="本地模型跳过意图分析")
    else:
        yield progress("正在识别您的意图...")
        intent = await analysis.recognize_intent(question, history, llm=llm)
    logger.info("intent_done", related=intent.related, intent=intent.intent)

    # ---- 3. 不相关：通用对话直答（不进 RAG，省检索成本）----
    if not intent.related:
        yield progress("正在为您生成回答...")
        answer = ""
        interrupted = False
        async for token in _general_chat(question, history, llm=llm):
            answer += token
            yield SseEvent.TOKEN, {"text": token}
            await flush_partial(answer)
            if stop_event.is_set():
                interrupted = True               # 用户中断：退出即关闭上游 LLM 流，部分答案照常持久化
                break
        await chat.update_assistant_content(assistant_msg_id, answer, model_name=model_name)
        asyncio.create_task(_maybe_summarize(user_id, conv_id))
        yield SseEvent.ANSWER_MESSAGE, {"message_id": assistant_msg_id}
        _suggestion = await _generate_suggestions(question, answer, llm=llm)
        if _suggestion:
            yield _suggestion
        yield SseEvent.DONE, {"conversation_id": conv_id, "message_id": assistant_msg_id}
        return

    # ---- 4. 相关：RAG 流程 ----
    if skip_analysis:
        rewritten = question
        route = RouteResult(strategy="knowledge_base", reasoning="本地模型默认知识库检索",
                            confidence=0.5)
        await chat.update_assistant_content(assistant_msg_id, "", transform_content=rewritten)
    else:
        yield progress("正在改写您的问题...")
        rewritten = await analysis.transform_query(question, history, llm=llm)
        await chat.update_assistant_content(assistant_msg_id, "", transform_content=rewritten)

        yield progress("正在路由您的问题...")
        route = await analysis.route_query(rewritten, _TABLE_HINT, llm=llm)

    card = None
    chunks: list[RetrievedChunk] = []
    references: list[dict] = []
    try:
        # 生效版本集合统一取一次：三个分支的检索都必须按同一份可见版本过滤
        active_versions = await document_service.active_version_ids()
        if route.strategy == "relational_db":
            yield progress("正在查询结构化数据...")
            chunks, refs, card = await text2sql.query_structured(
                rewritten, user_identity=user_identity, doc_versions=active_versions)
            references = [r.to_dict() for r in refs]
        elif route.strategy == "graph_db":
            yield progress("正在检索知识图谱...")
            chunks, refs, card = await graph.query_graph(
                rewritten, user_identity=user_identity, doc_versions=active_versions)
            references = [r.to_dict() for r in refs]
        else:
            yield progress("正在检索知识库...")
            chunks, refs = await retrieval.retrieve_and_aggregate(
                rewritten, user_identity=user_identity, doc_versions=active_versions)
            references = [r.to_dict() for r in refs]
    except Exception as e:
        logger.error("rag_retrieve_failed", error=str(e))
        yield SseEvent.ERROR, {"message": "检索暂时不可用，已按通用知识回答"}
        chunks, references = [], []

    if references:
        await chat.update_rag_references(assistant_msg_id, references)
        yield SseEvent.REFERENCE, {"references": references}
    if card:
        yield SseEvent.CARD, {"card": card}

    # ---- 5. 流式生成（[CARD:...] 协议块不外发，结束后解析为卡片事件）----
    yield progress("正在生成回答...")
    answer = ""
    visible = ""                     # 去掉卡片协议块后的可见正文（节流回填用）
    card_filter = _CardStreamFilter()
    interrupted = False
    async for token in _generate(intent.intent, question, rewritten, chunks, history,
                                 llm=llm):
        answer += token
        text = card_filter.feed(token)
        if text:
            visible += text
            yield SseEvent.TOKEN, {"text": text}
            await flush_partial(visible)
        if stop_event.is_set():
            interrupted = True                   # 用户中断：退出即关闭上游 LLM 流，不必等下一个 token
            break
    if not interrupted:
        tail = card_filter.flush()               # 收尾：残留正文外发，未闭合协议块丢弃
        if tail:
            yield SseEvent.TOKEN, {"text": tail}

    # 卡片协议块从正文剥离，解析为结构化卡片事件
    cards, answer = _extract_cards(answer)
    for c in cards:
        yield SseEvent.CARD, {"card": c}

    await chat.update_assistant_content(
        assistant_msg_id, answer, transform_content=rewritten,
        model_name=model_name)

    # 带图片的对话回答因多模态输入不可复现，不做缓存挖掘
    if not interrupted and not image_parts:
        asyncio.create_task(_mine_qa_cache(question, answer, chunks))

    asyncio.create_task(_maybe_summarize(user_id, conv_id))
    yield SseEvent.ANSWER_MESSAGE, {"message_id": assistant_msg_id}
    _suggestion = await _generate_suggestions(question, answer, llm=llm)
    if _suggestion:
        yield _suggestion
    yield SseEvent.DONE, {"conversation_id": conv_id, "message_id": assistant_msg_id}


# ------------------------------------------------------------------
# 各环节的 LLM 调用（均带降级）
# ------------------------------------------------------------------
_CARD_MARK = "[CARD:"


def _extract_cards(answer: str) -> tuple[list[dict], str]:
    """把回答中的 [CARD:{json}] 协议块解析为卡片列表，并从正文去除。

    花括号配对定界（JSON 内部可嵌套 {}，非贪婪正则会被多层嵌套坑）。
    """
    cards: list[dict] = []
    parts: list[str] = []
    i = 0
    while True:
        pos = answer.find(_CARD_MARK, i)
        if pos < 0:
            parts.append(answer[i:])
            break
        parts.append(answer[i:pos])
        start = answer.find("{", pos)
        if start < 0:
            break
        depth, j = 0, start
        in_str = False
        while j < len(answer):
            ch = answer[j]
            if in_str:                                   # 字符串内的花括号不计层
                if ch == '"' and answer[j - 1] != "\\":
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0 or j >= len(answer):               # JSON 未闭合：丢弃残块
            logger.warning("card_block_unclosed")
            break
        try:
            card = json.loads(answer[start:j + 1])
            if isinstance(card, dict):
                card = _normalize_card(card)
                data = card.get("data")
                # data 非空列表才下发事件，过滤无数据的 LLM 协议块，避免“只有标题+空表”噪声
                if isinstance(data, list) and data:
                    cards.append(card)
        except json.JSONDecodeError:
            logger.warning("card_json_unparsable", raw=answer[start:j + 1][:200])
        i = j + 1
        # 跳过紧随的 ] 收尾符
        if i < len(answer) and answer[i] == "]":
            i += 1
    cleaned = "".join(parts).strip()
    return cards, cleaned


def _normalize_card(card: dict) -> dict:
    """补齐 LLM 协议卡片的列名：卡片仅有 type/title/data 时按 data 首行键推导 columns。

    服务端自建卡（text2sql/graph）本就带 columns，此处只兜底 LLM 缺列情形，
    避免前端 v-for=columns 零列空表。
    """
    cols = card.get("columns")
    if isinstance(cols, list) and cols:
        return card
    data = card.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        card["columns"] = [str(k) for k in data[0].keys()]
    else:
        card.setdefault("columns", [])
    return card


def _marker_cut(text: str) -> int:
    """返回尾部属于 "[CARD:" 前缀的长度（流式 token 可能把标记拆断）。"""
    marker = "[CARD:"
    for k in range(min(len(marker) - 1, len(text)), 0, -1):
        if text.endswith(marker[:k]):
            return k
    return 0


class _CardStreamFilter:
    """流式输出时抑制 [CARD:{json}] 协议块：块内容不外发，闭合后回到正文。

    字符串感知的花括号配对状态机：JSON 内部可嵌套 {}、字符串值可含 "}" 或转义
    引号，均不影响定界；token 可任意拆断标记或 JSON 而不漏字。与 _extract_cards
    共用同一配对规则，避免流式抑制与终态解析逻辑漂移。
    """

    _MARKER = "[CARD:"

    def __init__(self) -> None:
        self._buf = ""        # 正文待外发缓冲（尾部可能挂起一段标记前缀）
        self._card = ""       # 协议块收集态原文（含 [CARD: 前缀）
        self._in_card = False

    def feed(self, token: str) -> str:
        """喂入一个 token，返回本次可安全外发的正文（可能为空串）。"""
        out: list[str] = []
        rest = token
        while True:
            if self._in_card:
                self._card += rest
                end = self._block_end(self._card)
                if end is None:                   # 尚未闭合：继续收集
                    return "".join(out)
                rest = self._card[end:]            # 闭合点之后的内容重新过一遍状态机
                self._card, self._in_card = "", False
                continue
            self._buf += rest
            pos = self._buf.find(self._MARKER)
            if pos >= 0:                           # 命中协议块：块前正文外发，进入收集态
                out.append(self._buf[:pos])
                self._card = self._buf[pos:]
                self._buf, self._in_card, rest = "", True, ""
                continue
            cut = _marker_cut(self._buf)           # 尾部是标记前缀则挂起，防半截标记泄漏
            if cut:
                out.append(self._buf[:len(self._buf) - cut])
                self._buf = self._buf[-cut:]
            else:
                out.append(self._buf)
                self._buf = ""
            return "".join(out)

    def flush(self) -> str:
        """流结束：外发残留正文；未闭合的协议块丢弃（已计入 answer，由 _extract_cards 兜底）。"""
        if self._in_card:
            return ""
        out, self._buf = self._buf, ""
        return out

    @classmethod
    def _block_end(cls, s: str) -> int | None:
        """s 以 [CARD: 开头。协议块完整闭合时返回块之后紧邻位置的下标，否则 None。"""
        start = s.find("{")
        if start < 0:
            return None                       # { 还没到（标记刚出现），不能误当作非块
        depth, in_str, esc = 0, False, False
        j = start
        while j < len(s):
            ch = s[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    k = j + 1
                    if k >= len(s):
                        return None            # } 在末尾、] 未确定，等下一 token
                    if s[k] == "]":
                        k += 1
                    return k
            j += 1
        return None


_IMG_MD_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def _extract_doc_images(chunks: list[RetrievedChunk], limit: int = 4) -> list[str]:
    """从检索分段中提取文档图片 URL（去重、限数）。"""
    seen: set[str] = set()
    urls: list[str] = []
    for c in chunks:
        for u in _IMG_MD_RE.findall(c.text or ""):
            if u in seen:
                continue
            seen.add(u)
            urls.append(u)
            if len(urls) >= limit:
                return urls
    return urls


async def _generate(intent: str, question: str, rewritten: str,
                    chunks: list[RetrievedChunk], history: str,
                    *, llm=None) -> AsyncIterator[str]:
    """基于参考资料流式生成。LLM 不可用 → 直接返回检索到的资料摘要兜底。

    若当前对话模型支持多模态（llm.supports_vision），且检索分段含文档图片，
    则把图片作为 image_url 一并发送；否则只传文字描述，避免服务端报错。
    """
    llm = llm or get_llm()
    if not llm.available:
        fallback = _no_llm_fallback(chunks)
        for i in range(0, len(fallback), 20):
            yield fallback[i:i + 20]
        return
    prompt = render("rag-answer-prompt", intent=intent,
                    contexts=_fmt_contexts(chunks), question=question)
    system = f"你是{intent}领域的资深技术专家。" + (
        f"\n\n# 历史对话\n{history}" if history else "")

    # 多模态：仅当模型支持识图且分段含图片时才发送图片
    doc_image_parts: list[dict] = []
    if getattr(llm, "supports_vision", False):
        img_urls = _extract_doc_images(chunks)
        if img_urls:
            doc_image_parts = await _load_image_parts(img_urls)
            logger.info("rag_doc_images_sent", count=len(doc_image_parts),
                        model=llm.chat_model)

    try:
        if doc_image_parts:
            messages = ([{"role": "system", "content": system}] if system else []) + \
                       [{"role": "user", "content": [
                           {"type": "text", "text": prompt}, *doc_image_parts]}]
            async for token in llm.stream_chat(None, system=system, temperature=0.5,
                                               messages=messages):
                yield token
        else:
            async for token in llm.stream_chat(prompt, system=system, temperature=0.5):
                yield token
    except Exception as e:
        logger.warning("generate_stream_failed", error=str(e))
        yield "\n（生成服务暂时不稳定，以上为检索到的关键资料摘要。）"


async def _vision_chat(question: str, history: str,
                       image_parts: list[dict], *, llm=None) -> AsyncIterator[str]:
    """图片理解：专用读图提示词，不注入知识库资料，避免小模型上下文溢出。

    视觉模型不可用/请求失败 → 给出可操作的配置提示，而不是空回答。"""
    llm = llm or get_llm()
    if not llm.available:
        yield "（图片理解服务未配置。请在「模型配置」中设置 purpose=vision 的视觉模型。）"
        return
    system = ("你是一名善于读图的编程技术知识助手。请仔细观察用户上传的图片："
              "先概括图片内容（界面截图/报错信息/图表/代码），再针对用户的问题给出结论；"
              "看不清或图片中不存在的信息不要编造，可以用『图片中未显示』说明。")
    prompt = question + (f"\n\n# 历史对话\n{history}" if history else "")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": [{"type": "text", "text": prompt}, *image_parts]},
    ]
    try:
        async for token in llm.stream_chat(prompt, system=system, temperature=0.3,
                                           messages=messages):
            yield token
    except Exception as e:
        logger.warning("vision_generate_failed", error=str(e))
        yield "（图片理解服务暂时不可用，请检查「模型配置」中的图片理解模型是否在线。）"


async def _general_chat(question: str, history: str, *, llm=None) -> AsyncIterator[str]:
    """与编程技术无关的闲聊：通用 LLM 直答。LLM 不可用 → 固定话术。"""
    llm = llm or get_llm()
    if not llm.available:
        yield "您好，我是编程知识助手。当前AI服务未配置，暂时无法为您生成个性化回答。"
        return
    system = "你是一名友好的编程技术知识助手，对与技术无关的闲聊做简短得体的回应，并引导用户咨询编程技术问题。"
    prompt = question + (f"\n\n# 历史\n{history}" if history else "")
    try:
        async for token in llm.stream_chat(prompt, system=system, temperature=0.7):
            yield token
    except Exception as e:
        logger.warning("general_chat_failed", error=str(e))
        yield "抱歉，服务暂时不可用，请稍后再试。"


async def _async_title(conversation_id: str, question: str) -> None:
    llm = get_llm()
    if not llm.available:
        return
    try:
        title = await llm.chat(render("title-summary-prompt", question=question),
                               model=llm.flash_model, temperature=0.7, max_tokens=32)
        if title and title.strip():
            await chat.update_title(conversation_id, title.strip())
    except Exception as e:
        logger.warning("title_summary_failed", error=str(e))


async def _maybe_summarize(user_id: str, conversation_id: str) -> None:
    """滚动摘要：未折叠消息达到阈值时，异步生成新摘要并落库。

    触发阈值 = summary_round_interval（轮次）* 2（每轮 user+assistant 两条）。
    长期记忆关闭时直接跳过。LLM 失败静默降级，不影响主流程。
    """
    try:
        pref = await chat.get_preference(user_id)
        if not pref.long_term_memory_enabled:
            return
        threshold = max(2, int(pref.summary_round_interval)) * 2
        unfolded = await chat.count_messages_since_summary(conversation_id)
        if unfolded < threshold:
            return
        prev = await chat.get_summary(conversation_id)
        prev_summary = prev.summary_text if prev and prev.summary_text else "（无）"
        # 取本轮待折叠的消息（最近 unfolded 条按正序）
        history_plain = await chat.get_history(conversation_id)  # 不带摘要，纯最近窗口
        llm = get_llm()
        if not llm.available:
            return
        new_summary = await llm.chat(
            render("conversation-summary-prompt",
                   prev_summary=prev_summary, new_messages=history_plain),
            model=llm.flash_model, temperature=0.3, max_tokens=1024)
        if new_summary and new_summary.strip():
            # 新摘要折叠了此前所有未折叠消息
            total_folded = (prev.message_count if prev else 0) + unfolded
            await chat.save_summary(conversation_id, new_summary.strip(), total_folded)
            logger.info("conversation_summarized", conv=conversation_id,
                        folded=total_folded, summary_len=len(new_summary))
    except Exception as e:
        logger.warning("conversation_summary_failed", error=str(e))


async def _generate_suggestions(question: str, answer: str, *, llm=None) -> tuple[str, dict] | None:
    """生成 3 条 follow-up 建议问题，返回 SUGGESTIONS 事件元组；失败返回 None。"""
    from app.models.constants import SseEvent
    llm = llm or get_llm()
    if not llm.available or not question or not answer:
        return None
    try:
        raw = await llm.chat(
            render("followup-suggestions-prompt", question=question[:500], answer=answer[:2000]),
            model=llm.flash_model, temperature=0.7, max_tokens=200)
        questions = [line.strip().lstrip("0123456789.、) ")
                     for line in (raw or "").splitlines() if line.strip()]
        questions = [q for q in questions if q][:3]
        if questions:
            return SseEvent.SUGGESTIONS, {"questions": questions}
    except Exception as e:
        logger.warning("followup_suggestions_failed", error=str(e))
    return None


async def _mine_qa_cache(question: str, answer: str, chunks: list[RetrievedChunk]) -> None:
    """对话成功后挖掘 QA 缓存候选（频次统计→达阈值入 PENDING 待审）。"""
    try:
        from app.modules.qa_cache import service as qa_cache
        sources = [{"documentId": str(c.document_id), "documentTitle": c.title,
                    "chunkId": c.chunk_id, "version": str(c.doc_version) if c.doc_version else None,
                    "content": c.text[:500]}
                   for c in chunks if c.document_id]
        await qa_cache.record_qa(question, answer, sources)
    except Exception as e:
        logger.warning("qa_cache_mine_failed", error=str(e))


def _no_llm_fallback(chunks: list[RetrievedChunk]) -> str:
    if not chunks:
        return "抱歉，未检索到相关资料，且AI生成服务当前不可用。"
    tops = "\n".join(f"• {c.text[:120]}" for c in chunks[:3])
    return f"已为您检索到以下相关资料（AI 摘要服务暂不可用）：\n{tops}"
