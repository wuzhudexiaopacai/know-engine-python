"""RBAC 权限字典与内置角色定义。

权限点由平台统一维护（permission 表），角色可自定义并绑定权限。
平台超管拥有通配权限 `*`，其余角色按权限点精确判定。
"""

from __future__ import annotations

from enum import StrEnum

ALL = "*"
PLATFORM_TENANT_ID = 0
DEFAULT_TENANT_ID = 1


class RoleCode(StrEnum):
    PLATFORM_ADMIN = "platform_admin"   # 平台超管：跨租户，可管理租户与全局配置
    TENANT_ADMIN = "tenant_admin"       # 租户管理员：本租户成员/角色/模型/文档/审核
    KB_ADMIN = "kb_admin"               # 知识库管理员：文档全生命周期
    REVIEWER = "reviewer"               # 审核员：问答缓存审核
    MEMBER = "member"                   # 普通成员：对话与检索


class PermCode(StrEnum):
    TENANT_MANAGE = "tenant:manage"
    MEMBER_MANAGE = "member:manage"
    ROLE_MANAGE = "role:manage"
    MODEL_MANAGE = "model:manage"
    DOC_READ = "document:read"
    DOC_WRITE = "document:write"
    DOC_DELETE = "document:delete"
    QA_REVIEW = "qa:review"
    CHAT_USE = "chat:use"
    CHAT_AUDIT = "chat:audit"
    LOG_VIEW = "log:view"


# (权限码, 名称, 模块) —— 启动时幂等写入 permission 表
PERMISSION_CATALOG: tuple[tuple[str, str, str], ...] = (
    (PermCode.TENANT_MANAGE, "租户管理", "platform"),
    (PermCode.MEMBER_MANAGE, "成员管理", "tenant"),
    (PermCode.ROLE_MANAGE, "角色管理", "tenant"),
    (PermCode.MODEL_MANAGE, "模型配置", "tenant"),
    (PermCode.DOC_READ, "文档查看", "document"),
    (PermCode.DOC_WRITE, "文档上传/切片", "document"),
    (PermCode.DOC_DELETE, "文档删除", "document"),
    (PermCode.QA_REVIEW, "问答审核", "qa_cache"),
    (PermCode.CHAT_USE, "智能问答", "chat"),
    (PermCode.CHAT_AUDIT, "会话审计", "chat"),
    (PermCode.LOG_VIEW, "日志中心", "observability"),
)

# (角色码, 角色名, 描述, 权限集合)
BUILTIN_ROLES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (RoleCode.PLATFORM_ADMIN, "平台超管", "跨租户管理租户、账号与平台配置", (ALL,)),
    (RoleCode.TENANT_ADMIN, "租户管理员", "管理本租户成员、角色、模型与知识库", (
        PermCode.MEMBER_MANAGE, PermCode.ROLE_MANAGE, PermCode.MODEL_MANAGE,
        PermCode.DOC_READ, PermCode.DOC_WRITE, PermCode.DOC_DELETE,
        PermCode.QA_REVIEW, PermCode.CHAT_USE, PermCode.CHAT_AUDIT,
        PermCode.LOG_VIEW,
    )),
    (RoleCode.KB_ADMIN, "知识库管理员", "维护知识文档与切片", (
        PermCode.DOC_READ, PermCode.DOC_WRITE, PermCode.DOC_DELETE, PermCode.CHAT_USE,
    )),
    (RoleCode.REVIEWER, "审核员", "审核问答缓存", (
        PermCode.QA_REVIEW, PermCode.DOC_READ, PermCode.CHAT_USE,
    )),
    (RoleCode.MEMBER, "普通成员", "对话、检索与查看文档", (
        PermCode.DOC_READ, PermCode.CHAT_USE,
    )),
)

ADMIN_ROLE_CODES = frozenset({
    RoleCode.PLATFORM_ADMIN, RoleCode.TENANT_ADMIN, RoleCode.KB_ADMIN, RoleCode.REVIEWER,
})