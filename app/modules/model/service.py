"""对话模型配置 Service。

设计要点：
- 一行配置 = 一个可被用户选中的模型，全部走 OpenAI 兼容协议（/chat/completions），
  本地 Ollama/vLLM 与阿里百炼、智谱、OpenAI 只是 base_url + model_name 不同
- 密钥落库但接口只回显脱敏串；编辑时 api_key 留空表示沿用旧密钥
- 同一时刻仅一个 is_default=1；解析模型时按"指定 id → 默认 → 首个启用 → 环境变量"降级
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx
from sqlalchemy import update

from app.config.settings import get_settings
from app.core.database import session_scope
from app.core.exceptions import NotFoundError, ParamError
from app.core.logging import get_logger
from app.models.entities import LlmModel, ModelRoleBinding
from app.models.queries import base_filter, soft_delete, update_with_lock
from app.modules.model.schemas import (
    ModelSaveRequest, ModelTestRequest, ModelTestResult, ModelVO, PublicModelVO,
    ModelRoleVO,
)
# 端点拼接/响应解析与运行期精排服务共用同一套规则，避免"测试通过但调用失败"
from app.services.rerank import build_rerank_body, parse_rerank, rerank_endpoint

logger = get_logger("app.model")

# 视觉模型连通性测试用的 1x1 PNG（只验证「能否接收图片输入」，不追求识别效果）
_TEST_IMAGE_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
                   "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

# 运行期模型描述：供 LLM 客户端按需构造（含明文密钥，仅在进程内流转）
class ModelRuntime:
    __slots__ = ("id", "display_name", "model_name", "base_url", "api_key",
                 "lock_version", "allow_empty_key", "provider", "supports_vision")

    def __init__(self, id: int, display_name: str, model_name: str,
                 base_url: str, api_key: str, lock_version: int,
                 allow_empty_key: bool = False, provider: str = "custom",
                 supports_vision: bool = False):
        self.id = id
        self.display_name = display_name
        self.model_name = model_name
        self.base_url = base_url
        self.api_key = api_key
        self.lock_version = lock_version
        self.allow_empty_key = allow_empty_key
        self.provider = provider
        self.supports_vision = supports_vision

    @classmethod
    def from_env(cls) -> "ModelRuntime":
        cfg = get_settings().llm
        return cls(id=0, display_name=f"默认模型（{cfg.chat_model}）",
                   model_name=cfg.chat_model, base_url=cfg.base_url,
                   api_key=cfg.api_key, lock_version=0,
                   allow_empty_key=_is_local_endpoint("custom", cfg.base_url))


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}


def _is_local_endpoint(provider: str, base_url: str) -> bool:
    """本地部署（Ollama/vLLM）通常不校验密钥，允许空 Key 直接调用。"""
    if provider == "local":
        return True
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOCAL_HOSTS


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def _to_vo(m: LlmModel) -> ModelVO:
    return ModelVO(
        id=m.id, purpose=m.purpose or "chat",
        provider=m.provider, display_name=m.display_name,
        model_name=m.model_name, base_url=m.base_url,
        api_key_masked=_mask(m.api_key or ""), has_api_key=bool(m.api_key),
        is_default=m.is_default, enabled=m.enabled, sort=m.sort,
        remark=m.remark, supports_vision=m.supports_vision,
        lock_version=m.lock_version, updated_at=m.updated_at,
    )


async def list_models() -> list[ModelVO]:
    async with session_scope() as session:
        rows = (await session.execute(
            base_filter(LlmModel)
            .order_by(LlmModel.sort, LlmModel.id))).scalars().all()
    return [_to_vo(m) for m in rows]


async def public_models() -> list[PublicModelVO]:
    """用户端可选模型：只列出启用且支持对话用途的模型；库里为空时回退到环境变量默认模型。"""
    async with session_scope() as session:
        rows = (await session.execute(
            base_filter(LlmModel).where(LlmModel.enabled == 1)
            .order_by(LlmModel.is_default.desc(), LlmModel.sort, LlmModel.id))).scalars().all()
    rows = [r for r in rows if _has_purpose(r, "chat")]
    if not rows:
        cfg = get_settings().llm
        if cfg.api_key:
            return [PublicModelVO(id=0, display_name=f"默认模型（{cfg.chat_model}）",
                                  model_name=cfg.chat_model, provider="aliyun", is_default=1)]
        return []
    return [PublicModelVO(id=m.id, display_name=m.display_name, model_name=m.model_name,
                          provider=m.provider, is_default=m.is_default) for m in rows]


# ==================================================================
# 模型用途（角色）：系统里所有会调用模型的位置
# ==================================================================
# capability = 该用途需要哪种 purpose 的模型；候选列表按此过滤
ROLE_SPECS: dict[str, dict[str, str]] = {
    "chat": {
        "label": "对话模型",
        "capability": "chat",
        "description": ("用户提问时的意图识别、问题改写与回答生成。用户在对话页可临时选择其他"
                        "对话模型；未选择或未配置绑定时按此用途生效。"),
    },
    "vision": {
        "label": "图片理解",
        "capability": "vision",
        "description": ("文档解析时为图片生成描述（进入知识库可检索文本），"
                        "以及对话中用户上传图片时的看图回答。必须是支持图片输入的视觉模型。"),
    },
    "speech": {
        "label": "语音识别",
        "capability": "speech",
        "description": "对话页录音转文字（语音输入）。未配置时语音输入不可用。",
    },
    "embedding": {
        "label": "文本向量化",
        "capability": "embedding",
        "description": ("文档入库与问题检索时的向量化模型。更换后向量维度/语义空间会变化，"
                        "旧文档需要重新向量化才能与新模型匹配。"),
    },
    "rerank": {
        "label": "检索精排",
        "capability": "rerank",
        "description": ("检索结果的相关性精排。未配置或调用失败时自动降级为多路召回排名融合"
                        "（RRF）＋词面命中率校验。"),
    },
    "image_gen": {
        "label": "文生图",
        "capability": "image_gen",
        "description": "对话页「生成图片」功能使用的绘画模型。",
    },
    "memory": {
        "label": "记忆摘要",
        "capability": "chat",
        "description": ("长期记忆的后台小任务：滚动摘要、会话智能标题、新会话推荐。"
                        "可单独绑定一个更轻量的对话模型以降低开销；"
                        "未绑定时自动回退到对话模型的解析链。"),
    },
}


def runtime_of(row: LlmModel) -> ModelRuntime:
    return ModelRuntime(row.id, row.display_name, row.model_name,
                        row.base_url, row.api_key or "", row.lock_version,
                        _is_local_endpoint(row.provider, row.base_url), row.provider,
                        bool(getattr(row, "supports_vision", 0)))


def _purposes(row: LlmModel) -> set[str]:
    """解析模型支持的用途集合：purpose 字段存逗号分隔的多值，如 'chat,vision'。"""
    return {p.strip() for p in (row.purpose or "chat").split(",") if p.strip()}


def _has_purpose(row: LlmModel, purpose: str) -> bool:
    return purpose in _purposes(row)


async def _enabled_by_purpose(session, purpose: str) -> LlmModel | None:
    rows = (await session.execute(
        base_filter(LlmModel).where(LlmModel.enabled == 1)
        .order_by(LlmModel.is_default.desc(), LlmModel.sort, LlmModel.id))).scalars().all()
    for r in rows:
        if _has_purpose(r, purpose):
            return r
    return None


async def resolve_runtime(model_id: int | None) -> ModelRuntime:
    """把前端选择的 model_id 解析为可调用的运行期配置。

    model_id 显式指定但不存在/停用 → 报错；未指定 → 用途绑定 → 默认 → 首个启用 → 环境变量。
    """
    async with session_scope() as session:
        if model_id:
            row = (await session.execute(
                base_filter(LlmModel).where(LlmModel.id == model_id))).scalar_one_or_none()
            if row is None:
                raise ParamError("所选模型不存在或已被删除")
            if row.enabled != 1:
                raise ParamError("所选模型已停用，请选择其他模型")
            return runtime_of(row)

        bound = await _bound_model(session, "chat")
        if bound is not None:
            return runtime_of(bound)
        row = await _enabled_by_purpose(session, "chat")
        if row is not None:
            return runtime_of(row)
    return ModelRuntime.from_env()


async def resolve_vision_runtime() -> ModelRuntime | None:
    """图片理解专用模型：用途绑定优先，其次 purpose='vision' 的默认项。

    未配置时返回 None（调用方跳过图片描述），绝不静默降级到对话主模型：
    对话模型通常不支持图片输入，静默降级只会让每张图都失败重试。
    """
    return await resolve_role_runtime("vision")


async def resolve_memory_runtime() -> ModelRuntime:
    """长期记忆后台任务（滚动摘要 / 智能标题 / 新会话推荐）专用模型。

    绑定优先；未绑定时回退到对话模型的完整解析链（chat 用途绑定 → chat 默认
    → 首个启用 → 环境变量），保证新增此用途不改变任何既有行为。
    """
    async with session_scope() as session:
        bound = await _bound_model(session, "memory")
        if bound is not None:
            return runtime_of(bound)
    return await resolve_runtime(None)


async def _bound_model(session, role: str) -> LlmModel | None:
    """读取角色绑定并校验模型仍启用；绑定失效时返回 None，由调用方走默认降级。"""
    binding = (await session.execute(
        base_filter(ModelRoleBinding).where(ModelRoleBinding.role == role)
    )).scalars().first()
    if binding is None or not binding.model_id:
        return None
    row = (await session.execute(
        base_filter(LlmModel).where(LlmModel.id == binding.model_id))).scalar_one_or_none()
    if row is None or row.enabled != 1:
        logger.warning("role_binding_invalid", role=role, model_id=binding.model_id)
        return None
    return row


async def resolve_role_runtime(role: str) -> ModelRuntime | None:
    """按系统用途解析模型：绑定 → 同用途默认模型 → None（调用方用环境变量兜底）。"""
    spec = ROLE_SPECS.get(role)
    if spec is None:
        return None
    async with session_scope() as session:
        bound = await _bound_model(session, role)
        if bound is not None:
            return runtime_of(bound)
        row = await _enabled_by_purpose(session, spec["capability"])
    return runtime_of(row) if row is not None else None


def _env_fallback(role: str) -> str | None:
    """未绑定且无同用途模型时，实际使用的环境变量配置说明（仅供后台展示）。"""
    s = get_settings()
    if role == "chat":
        return f"环境变量默认对话模型（{s.llm.chat_model}）" if s.llm.api_key else None
    if role == "vision":
        return None
    if role == "speech":
        return "环境变量语音识别配置" if (s.speech.api_key or s.llm.api_key) else None
    if role == "embedding":
        return f"环境变量默认向量模型（{s.llm.embedding_model}）" if s.llm.api_key else None
    if role == "rerank":
        return f"环境变量精排配置（{s.rerank.model}）" if s.llm.api_key else None
    if role == "image_gen":
        return f"环境变量文生图模型（{s.image.model}）" if (s.image.api_key or s.llm.api_key) else None
    if role == "memory":
        return f"对话模型解析链（当前 {s.llm.chat_model}）" if s.llm.api_key else "对话模型解析链"
    return None


async def list_roles() -> list[ModelRoleVO]:
    """后台「模型用途」列表：每个用途的说明、当前绑定、候选模型。"""
    async with session_scope() as session:
        bindings = {b.role: b.model_id for b in (await session.execute(
            base_filter(ModelRoleBinding))).scalars().all()}
        rows = (await session.execute(
            base_filter(LlmModel).where(LlmModel.enabled == 1)
            .order_by(LlmModel.sort, LlmModel.id))).scalars().all()
        rows_by_id = {r.id: r for r in rows}

    result: list[ModelRoleVO] = []
    for role, spec in ROLE_SPECS.items():
        candidates = [r for r in rows if _has_purpose(r, spec["capability"])]
        bound_id = bindings.get(role)
        bound = rows_by_id.get(bound_id) if bound_id else None
        result.append(ModelRoleVO(
            role=role, label=spec["label"], description=spec["description"],
            capability=spec["capability"],
            model=PublicModelVO(id=bound.id, display_name=bound.display_name,
                                model_name=bound.model_name, provider=bound.provider,
                                is_default=bound.is_default) if bound else None,
            candidates=[PublicModelVO(id=r.id, display_name=r.display_name,
                                      model_name=r.model_name, provider=r.provider,
                                      is_default=r.is_default) for r in candidates],
            fallback=None if bound else _env_fallback(role),
        ))
    return result


async def bind_role(role: str, model_id: int | None) -> None:
    """绑定/解绑模型用途；model_id=None 表示解绑，回退默认模型或环境变量。"""
    spec = ROLE_SPECS.get(role)
    if spec is None:
        raise ParamError("未知的模型用途")
    async with session_scope() as session:
        if model_id is not None:
            row = (await session.execute(
                base_filter(LlmModel).where(LlmModel.id == model_id))).scalar_one_or_none()
            if row is None:
                raise ParamError("所选模型不存在或已被删除")
            if row.enabled != 1:
                raise ParamError("所选模型已停用")
            if not _has_purpose(row, spec["capability"]):
                raise ParamError("该模型的能力类型与用途不匹配，请选择对应用途的模型")
        binding = (await session.execute(
            base_filter(ModelRoleBinding).where(ModelRoleBinding.role == role)
        )).scalars().first()
        if binding is None:
            session.add(ModelRoleBinding(role=role, model_id=model_id))
        else:
            binding.model_id = model_id
    logger.info("role_bound", role=role, model_id=model_id)


async def _clear_other_defaults(session, keep_id: int | None, purpose: str) -> None:
    """默认模型按用途分别唯一：含该 purpose 的其他模型取消默认标记。

    purpose 可能是逗号分隔的多值（如 'chat,vision'），逐个清除。
    """
    targets = {p.strip() for p in purpose.split(",") if p.strip()}
    if not targets:
        return
    rows = (await session.execute(
        base_filter(LlmModel).where(LlmModel.is_default == 1))).scalars().all()
    for r in rows:
        if keep_id is not None and r.id == keep_id:
            continue
        if _purposes(r) & targets:
            r.is_default = 0


async def create_model(req: ModelSaveRequest) -> ModelVO:
    async with session_scope() as session:
        if req.is_default:
            await _clear_other_defaults(session, None, req.purpose)
        row = LlmModel(
            purpose=req.purpose,
            provider=req.provider, display_name=req.display_name,
            model_name=req.model_name, base_url=req.base_url.rstrip("/"),
            api_key=req.api_key, is_default=1 if req.is_default else 0,
            enabled=1 if req.enabled else 0, sort=req.sort, remark=req.remark,
            supports_vision=1 if req.supports_vision else 0,
        )
        session.add(row)
        await session.flush()
        # created_at/updated_at 为服务端默认值，flush 后需刷新才能读到
        await session.refresh(row)
        vo = _to_vo(row)
    return vo


async def update_model(model_id: int, req: ModelSaveRequest, lock_version: int) -> ModelVO:
    async with session_scope() as session:
        row = (await session.execute(
            base_filter(LlmModel).where(LlmModel.id == model_id))).scalar_one_or_none()
        if row is None:
            raise NotFoundError("模型配置不存在")
        if req.is_default:
            await _clear_other_defaults(session, model_id, req.purpose)
        values = {
            "purpose": req.purpose,
            "provider": req.provider, "display_name": req.display_name,
            "model_name": req.model_name, "base_url": req.base_url.rstrip("/"),
            "is_default": 1 if req.is_default else 0,
            "enabled": 1 if req.enabled else 0,
            "sort": req.sort, "remark": req.remark,
            "supports_vision": 1 if req.supports_vision else 0,
        }
        # 留空表示沿用旧密钥，避免编辑表单回显明文
        if req.api_key:
            values["api_key"] = req.api_key
        await update_with_lock(session, LlmModel, "id", model_id, values, lock_version)
        refreshed = (await session.execute(
            base_filter(LlmModel).where(LlmModel.id == model_id))).scalar_one()
        vo = _to_vo(refreshed)
    return vo


async def delete_model(model_id: int, lock_version: int) -> None:
    async with session_scope() as session:
        row = (await session.execute(
            base_filter(LlmModel).where(LlmModel.id == model_id))).scalar_one_or_none()
        if row is None:
            raise NotFoundError("模型配置不存在")
        await soft_delete(session, LlmModel, "id", model_id, lock_version)


async def test_connection(req: ModelTestRequest) -> ModelTestResult:
    """最小化连通性测试：发一条小补全请求，验证地址/密钥/模型名三者可用。"""
    api_key = req.api_key
    provider = "custom"
    if req.id:
        async with session_scope() as session:
            row = (await session.execute(
                base_filter(LlmModel).where(LlmModel.id == req.id))).scalar_one_or_none()
            if row is not None:
                provider = row.provider
                if not api_key:
                    api_key = row.api_key or ""
    allow_empty = _is_local_endpoint(provider, req.base_url)
    if not api_key and not allow_empty:
        return ModelTestResult(ok=False, latency_ms=0,
                               message="未配置 API Key（本地部署可留空）")

    # 非对话类模型走各自协议的最小请求（对话端点对它们必然 400/404）
    # purpose 可能是逗号分隔的多值，取第一个匹配项决定测试方式
    purposes = {p.strip() for p in (req.purpose or "chat").split(",") if p.strip()}
    if "embedding" in purposes:
        return await _test_embedding(req, api_key)
    if "rerank" in purposes:
        return await _test_rerank(req, api_key)
    if "speech" in purposes:
        return await _test_speech(req, api_key)
    if purposes == {"image_gen"}:
        return ModelTestResult(ok=True, latency_ms=0,
                               message="文生图需真实生成才能验证，请保存后在对话页实测")

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    # 本地模型可能冷启动加载权重，给更长的等待
    timeout = httpx.Timeout(60.0, connect=8.0) if allow_empty else httpx.Timeout(15.0, connect=8.0)
    # 图片理解用途额外带一张 1x1 图：纯文本模型会直接 400，模型名不存在会 404
    content: object = "ping"
    if "vision" in purposes:
        content = [
            {"type": "text", "text": "描述这张图片"},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + _TEST_IMAGE_B64}},
        ]
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{req.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={"model": req.model_name, "max_tokens": 8,
                      "messages": [{"role": "user", "content": content}]},
            )
        elapsed = int((time.perf_counter() - started) * 1000)
        if resp.status_code == 200:
            msg = "连通正常（已通过图片输入校验）" if "vision" in purposes else "连通正常"
            return ModelTestResult(ok=True, latency_ms=elapsed, message=msg)
        detail = resp.text[:300].replace("\n", " ")
        return ModelTestResult(ok=False, latency_ms=elapsed,
                               message=f"HTTP {resp.status_code}: {detail}")
    except Exception as e:
        elapsed = int((time.perf_counter() - started) * 1000)
        logger.warning("model_test_failed", base_url=req.base_url, error=str(e))
        return ModelTestResult(ok=False, latency_ms=elapsed, message=f"连接失败: {e}")


async def _test_embedding(req: ModelTestRequest, api_key: str) -> ModelTestResult:
    """向量模型：请求 /embeddings 并校验返回了向量。"""
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
            resp = await client.post(
                f"{req.base_url.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                json={"model": req.model_name, "input": ["ping"]},
            )
        elapsed = int((time.perf_counter() - started) * 1000)
        if resp.status_code == 200:
            data = resp.json().get("data") or []
            dim = len(data[0].get("embedding") or []) if data else 0
            return ModelTestResult(ok=True, latency_ms=elapsed,
                                   message=f"向量化正常（维度 {dim}）")
        return ModelTestResult(ok=False, latency_ms=elapsed,
                               message=f"HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        elapsed = int((time.perf_counter() - started) * 1000)
        return ModelTestResult(ok=False, latency_ms=elapsed, message=f"连接失败: {e}")


async def _test_rerank(req: ModelTestRequest, api_key: str) -> ModelTestResult:
    """精排模型：请求配置地址对应的 rerank 端点（本地服务补 /rerank，不回落环境变量）。"""
    url = rerank_endpoint(req.base_url)
    body = build_rerank_body(url, req.model_name, "ping", ["ping", "pong"], 1)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
            resp = await client.post(
                url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                json=body)
            if resp.status_code in (400, 422) and "documents" in body:
                # TEI 只认 "texts" 字段
                body = {k: v for k, v in body.items() if k != "documents"} | {"texts": ["ping", "pong"]}
                resp = await client.post(
                    url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                    json=body)
        elapsed = int((time.perf_counter() - started) * 1000)
        if resp.status_code == 200:
            pairs = parse_rerank(resp.json())
            if pairs:
                return ModelTestResult(ok=True, latency_ms=elapsed, message="精排正常")
            return ModelTestResult(ok=False, latency_ms=elapsed,
                                   message=f"接口返回 200 但无法解析分数: {resp.text[:200]}")
        return ModelTestResult(ok=False, latency_ms=elapsed,
                               message=f"HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        elapsed = int((time.perf_counter() - started) * 1000)
        return ModelTestResult(ok=False, latency_ms=elapsed, message=f"连接失败: {e}")


def _silent_wav_b64() -> str:
    """0.2 秒 8kHz 单声道 16bit 静音 WAV：只为验证语音模型可接收音频输入。"""
    import base64
    import struct

    frames = 1600
    data = struct.pack("<%dh" % frames, *([0] * frames))
    header = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
              + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
              + b"data" + struct.pack("<I", len(data)))
    return base64.b64encode(header + data).decode()


async def _test_speech(req: ModelTestRequest, api_key: str) -> ModelTestResult:
    """语音识别模型：发送一小段静音音频，验证模型名与鉴权可用。"""
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=8.0)) as client:
            resp = await client.post(
                f"{req.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                json={"model": req.model_name, "stream": False, "messages": [
                    {"role": "user", "content": [
                        {"type": "input_audio",
                         "input_audio": {"data": f"data:;base64,{_silent_wav_b64()}",
                                         "format": "wav"}},
                    ]},
                ]},
            )
        elapsed = int((time.perf_counter() - started) * 1000)
        if resp.status_code == 200:
            return ModelTestResult(ok=True, latency_ms=elapsed, message="语音识别正常")
        return ModelTestResult(ok=False, latency_ms=elapsed,
                               message=f"HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        elapsed = int((time.perf_counter() - started) * 1000)
        return ModelTestResult(ok=False, latency_ms=elapsed, message=f"连接失败: {e}")
