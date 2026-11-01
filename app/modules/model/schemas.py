"""模型配置的入参/出参契约。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# 供应商枚举：全部走 OpenAI 兼容协议，差异只体现在 base_url 与模型名
PROVIDER_PATTERN = "^(aliyun|zhipu|bailian|openai|local|custom)$"
# 用途枚举（模型能力类型）：决定该模型可被哪些系统用途（角色）选用
# chat=对话；vision=图片理解；speech=语音识别；embedding=向量化；
# rerank=精排；image_gen=文生图
# 支持逗号分隔多值，如 'chat,vision'
_PURPOSE_ITEM = r"(chat|vision|speech|embedding|rerank|image_gen)"
PURPOSE_PATTERN = rf"^{_PURPOSE_ITEM}(,{_PURPOSE_ITEM})*$"


class ModelSaveRequest(BaseModel):
    """新增/编辑模型配置（api_key 留空表示编辑时沿用旧密钥）。"""

    purpose: str = Field(default="chat", pattern=PURPOSE_PATTERN)
    provider: str = Field(default="custom", pattern=PROVIDER_PATTERN)
    display_name: str = Field(min_length=1, max_length=64, description="用户端展示名")
    model_name: str = Field(min_length=1, max_length=128, description="上游真实模型名")
    base_url: str = Field(min_length=1, max_length=512)
    api_key: str = Field(default="", max_length=512)
    is_default: bool = False
    enabled: bool = True
    sort: int = Field(default=0, ge=0, le=999)
    remark: str | None = Field(default=None, max_length=512)
    supports_vision: bool = Field(default=False, description="是否支持图片输入（多模态）")


class ModelVO(BaseModel):
    """后台列表视图：密钥脱敏，只回显是否已配置。"""

    id: int
    purpose: str
    provider: str
    display_name: str
    model_name: str
    base_url: str
    api_key_masked: str
    has_api_key: bool
    is_default: int
    enabled: int
    sort: int
    remark: str | None = None
    supports_vision: int = 0
    lock_version: int
    updated_at: datetime


class PublicModelVO(BaseModel):
    """用户端可选模型：只暴露展示名与供应商，绝不外泄地址与密钥。"""

    id: int
    display_name: str
    model_name: str
    provider: str
    is_default: int


class ModelTestRequest(BaseModel):
    """连通性测试：可测未保存的表单，也可测已保存配置（api_key 留空则用库里的）。"""

    id: int | None = None
    base_url: str = Field(min_length=1, max_length=512)
    model_name: str = Field(min_length=1, max_length=128)
    api_key: str = Field(default="", max_length=512)
    # vision 用途会额外发送一张小图，提前暴露「模型不支持图片」或「模型不存在」
    # embedding/rerank 走各自协议的最小请求，其余用途仅校验对话端点
    purpose: str = Field(default="chat", pattern=PURPOSE_PATTERN)


class ModelTestResult(BaseModel):
    ok: bool
    latency_ms: int
    message: str


class RoleBindRequest(BaseModel):
    """模型用途绑定：model_id 为空表示解绑，回退到默认模型/环境变量。"""

    model_id: int | None = None


class ModelRoleVO(BaseModel):
    """系统用途（角色）视图：谁在用模型、当前绑定哪个、还能选哪些。"""

    role: str
    label: str
    description: str
    capability: str                  # 需要哪种用途（purpose）的模型
    model: PublicModelVO | None = None
    candidates: list[PublicModelVO] = Field(default_factory=list)
    fallback: str | None = None      # 未绑定时实际生效的来源说明
