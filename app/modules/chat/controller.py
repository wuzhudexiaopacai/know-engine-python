"""对话 REST + SSE 流式接口。

除问答外还承载三个能力入口：
- POST /interrupt：会话级真中断（置位停止信号，后端断开上游 LLM 流并持久化部分答案）
- POST /speech/transcribe：语音转写（multipart 音频 → 文本）
- POST /image：文生图（异步任务同步等待，结果以 markdown 图片消息落库）
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, get_current_user
from app.core.exceptions import ParamError
from app.core.logging import get_logger
from app.core.response import R
from app.infra import stop_signal
from app.models.constants import SseEvent
from app.modules.chat import service as chat
from app.modules.rag import pipeline
from app.services.image import ImageCancelled, ImageError, get_image
from app.services.speech import SpeechError, detect_audio_format, get_speech

logger = get_logger("app.chat.controller")

router = APIRouter(prefix="/api/chat", tags=["对话"])


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None
    use_cache: bool = True
    model_id: int | None = Field(default=None, description="管理员配置的对话模型 id，空则用默认模型")
    images: list[str] = Field(default_factory=list, max_length=4,
                              description="已上传图片的访问地址（/api/files/...），最多 4 张")


def _sse(event: str, data: dict) -> str:
    """SSE 帧编码：event 名 + JSON data（前端按 addEventListener 分派）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# 客户端断开后仍继续跑的生产任务强引用（防止被 GC；任务结束自动移除）
_background_tasks: set[asyncio.Task] = set()


@router.post("/stream")
async def stream_chat(body: ChatRequest,
                      user: CurrentUser = Depends(get_current_user)) -> StreamingResponse:
    """流式对话主接口。前端用 fetch + ReadableStream 逐块解析 SSE 帧。

    生成与下发解耦：LLM 生成跑在独立协程里，HTTP 连接只负责转发事件。
    浏览器关闭/刷新导致 SSE 断开时，生产协程不会被取消，回答继续生成并回填数据库，
    刷新后可按 conversation_id 走 /stream/attach 续流（事件同时写入会话缓冲）。
    """
    images = [u for u in body.images if u.startswith("/api/files/")][:4]
    if body.images:
        from app.modules.model import service as model_service
        if await model_service.resolve_vision_runtime() is None:
            raise ParamError("请先在「模型配置」中启用图片理解模型后再发送图片")

    agen = pipeline.stream_chat(
        user_id=user.owner_key, conversation_id=body.conversation_id,
        question=body.question, user_identity=user.identity_for_retrieval,
        use_cache=body.use_cache, model_id=body.model_id, images=images)

    async def encode():
        queue: asyncio.Queue = asyncio.Queue()
        sentinel = object()
        client_alive = True

        async def produce():
            try:
                async for event, data in agen:
                    # 客户端已断开：只保留落库副作用，不再堆积无人消费的 SSE 事件
                    if client_alive:
                        queue.put_nowait((event, data))
            except Exception as e:  # 生成器兜底：未捕获异常转 error 事件 + done 收尾
                logger.error("stream_generate_failed", error=str(e))
                if client_alive:
                    queue.put_nowait((SseEvent.ERROR, {"message": "服务异常，请稍后重试"}))
                    queue.put_nowait((SseEvent.DONE, {"conversation_id": "", "message_id": ""}))
            finally:
                queue.put_nowait(sentinel)

        task = asyncio.create_task(produce())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                yield _sse(*item)
        finally:
            client_alive = False

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
               "Connection": "keep-alive"}
    return StreamingResponse(encode(), media_type="text/event-stream; charset=utf-8",
                             headers=headers)


@router.get("/stream/attach")
async def attach_stream(conversation_id: str = Query(...),
                        user: CurrentUser = Depends(get_current_user)) -> StreamingResponse:
    """刷新/重连后继续接收进行中的生成：回放已缓冲事件并跟踪到本轮结束。

    会话当前没有生成任务时立即返回 idle 事件，前端保留已加载的历史消息。
    """
    await chat.verify_conversation_access(conversation_id, user.owner_key,
                                          is_admin=user.identity == "admin")

    async def encode():
        from app.infra import chat_stream as hub
        if not hub.is_active(conversation_id):
            yield _sse("idle", {"conversation_id": conversation_id})
            return
        async for event, data in hub.follow(conversation_id):
            if event == hub.HEARTBEAT:
                yield ": keep-alive\n\n"
                continue
            yield _sse(event, data)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
               "Connection": "keep-alive"}
    return StreamingResponse(encode(), media_type="text/event-stream; charset=utf-8",
                             headers=headers)


@router.post("/images")
async def upload_chat_image(file: UploadFile = File(...),
                            user: CurrentUser = Depends(get_current_user)) -> R:
    """对话图片上传：校验类型与大小后入对象存储，返回可回读的稳定地址。"""
    from app.infra.file_storage import get_file_store
    data = await file.read()
    if not file.content_type or not file.content_type.startswith("image/"):
        raise ParamError("仅支持上传图片（png/jpg/webp 等）")
    if not data:
        raise ParamError("图片内容为空")
    if len(data) > 5 * 1024 * 1024:
        raise ParamError("单张图片不能超过 5MB")
    key = await get_file_store().put("chat-images", file.filename or "image.png", data)
    return R.ok({"url": f"/api/files/{key}"}, "图片已上传")


@router.get("/models")
async def available_models(user: CurrentUser = Depends(get_current_user)) -> R:
    """当前可用的对话模型（管理员在后台启用后，用户端按展示名选择）。"""
    from app.modules.model import service as model_service
    return R.ok(await model_service.public_models())


@router.get("/references/{chunk_id}")
async def reference_detail(chunk_id: str,
                           user: CurrentUser = Depends(get_current_user)) -> R:
    """引用片段定位预览：返回文档、章节路径、段序与片段全文。"""
    return R.ok(await chat.reference_detail(chunk_id))


@router.get("/conversations")
async def list_conversations(scope: str = Query("self", pattern="^(self|all)$"),
                             user: CurrentUser = Depends(get_current_user)) -> R:
    """会话列表：默认仅本人；管理员传 scope=all 可查看全部会话。"""
    include_all = scope == "all" and user.identity == "admin"
    return R.ok(await chat.list_conversations(user.owner_key, include_all))


@router.get("/messages")
async def list_messages(conversation_id: str = Query(...),
                        user: CurrentUser = Depends(get_current_user)) -> R:
    return R.ok(await chat.list_messages(conversation_id, user.owner_key,
                                         is_admin=user.identity == "admin"))


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str,
                              user: CurrentUser = Depends(get_current_user)) -> R:
    await chat.delete_conversation(conversation_id, user.owner_key,
                                   is_admin=user.identity == "admin")
    return R.ok(None, "会话已删除")


class FeedbackRequest(BaseModel):
    message_id: str
    helpful: bool
    comment: str | None = Field(default=None, max_length=1000)


@router.post("/feedback")
async def feedback(body: FeedbackRequest, user: CurrentUser = Depends(get_current_user)) -> R:
    """答案点赞/点踩（负反馈联动 QA 缓存准出）。"""
    await chat.feedback(body.message_id, body.helpful, body.comment,
                        user.owner_key, is_admin=user.identity == "admin")
    return R.ok(None, "感谢您的反馈")


class InterruptRequest(BaseModel):
    conversation_id: str = Field(min_length=1)


@router.post("/interrupt")
async def interrupt(body: InterruptRequest,
                    user: CurrentUser = Depends(get_current_user)) -> R:
    """真中断：置位会话停止信号，生成协程在 token 边界退出并保留部分答案。

    返回 interrupted=False 仅表示该会话当前无进行中的流（幂等，前端可照常断开连接）。
    """
    await chat.verify_conversation_access(body.conversation_id, user.owner_key,
                                          is_admin=user.identity == "admin")
    found = stop_signal.request_stop(body.conversation_id)
    logger.info("chat_interrupt", user_id=user.sub, conv=body.conversation_id, found=found)
    return R.ok({"interrupted": found})


@router.post("/speech/transcribe")
async def transcribe(file: UploadFile = File(...),
                     user: CurrentUser = Depends(get_current_user)) -> R:
    """语音转写：前端将录音解码为 WAV 后以 multipart 上传，返回识别文本。"""
    audio = await file.read()
    if not audio:
        return R.fail(4000, "音频内容为空")
    fmt = detect_audio_format(file.content_type or "")
    try:
        text = await get_speech().transcribe(audio, fmt)
    except SpeechError as e:
        return R.fail(5000, str(e))
    return R.ok({"text": text})


class ImageGenRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=800)
    conversation_id: str | None = None
    size: str | None = None
    request_id: str | None = Field(default=None, max_length=64,
                                   description="前端生成的一次性请求标识，用于中断")


class ImageCancelRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)


def _image_stop_key(owner_key: str, request_id: str) -> str:
    """文生图中断信号键：按用户隔离，避免猜到他人 request_id 误停。"""
    return f"image:{owner_key}:{request_id}"


@router.post("/image/cancel")
async def cancel_image(body: ImageCancelRequest,
                       user: CurrentUser = Depends(get_current_user)) -> R:
    """中断进行中的文生图：轮询边界退出，图片不落库（可改完提示词重新生成）。"""
    cancelled = stop_signal.request_stop(_image_stop_key(user.owner_key, body.request_id))
    logger.info("image_cancel", user_id=user.sub, found=cancelled)
    return R.ok({"cancelled": cancelled})


@router.post("/image")
async def generate_image(body: ImageGenRequest,
                         user: CurrentUser = Depends(get_current_user)) -> R:
    """文生图：同步等待异步任务（前端按请求级超时展示加载态）。

    成功后把提问与图片 markdown 各落一条消息，刷新会话历史仍可见；
    被前端中断（/image/cancel）则抛 ImageCancelled，不产生任何消息。
    """
    key = _image_stop_key(user.owner_key, body.request_id) if body.request_id else ""
    stop_event = stop_signal.register(key) if key else None
    try:
        urls = await get_image().generate(body.prompt, size=body.size,
                                          stop_event=stop_event)
    except ImageCancelled:
        return R.fail(4000, "已停止生成")
    except ImageError as e:
        return R.fail(5000, str(e))
    finally:
        if key:
            stop_signal.unregister(key)

    conv_id, _ = await chat.ensure_conversation(user.owner_key, body.conversation_id, body.prompt)
    md = "\n\n".join(f"![{body.prompt[:40]}]({u})" for u in urls)
    user_msg_id = await chat.save_user_message(conv_id, f"【生成图片】{body.prompt}")
    assistant_msg_id = await chat.save_assistant_message(conv_id)
    await chat.update_assistant_content(assistant_msg_id, md)
    return R.ok({
        "conversation_id": conv_id,
        "urls": urls,
        "user_message": {"message_id": user_msg_id, "role": "user", "content": body.prompt},
        "assistant_message": {"message_id": assistant_msg_id, "content": md},
    })
