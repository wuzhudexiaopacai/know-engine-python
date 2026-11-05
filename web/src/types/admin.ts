// 后台管理：模型配置与普通用户账号

/** 管理员配置的模型（密钥脱敏） */
export interface ModelVO {
  id: number
  /** 用途（能力类型）：chat/vision/speech/embedding/rerank/image_gen */
  purpose: string
  provider: string
  display_name: string
  model_name: string
  base_url: string
  api_key_masked: string
  has_api_key: boolean
  is_default: number
  enabled: number
  sort: number
  remark: string | null
  supports_vision: number
  lock_version: number
  updated_at: string
}

/** 新增/编辑模型表单 */
export interface ModelSavePayload {
  purpose: string | string[]
  provider: string
  display_name: string
  model_name: string
  base_url: string
  api_key: string
  is_default: boolean
  enabled: boolean
  sort: number
  remark?: string | null
  supports_vision?: boolean
}

export interface ModelTestPayload {
  id?: number | null
  base_url: string
  model_name: string
  api_key?: string
  /** vision 额外发一张小图；embedding/rerank/speech 走各自协议的最小请求 */
  purpose?: string
}

/** 模型用途（系统位置）视图：谁在用模型、当前绑定、候选模型 */
export interface ModelRoleVO {
  role: string
  label: string
  description: string
  /** 该用途需要的能力类型，对应模型的 purpose */
  capability: string
  model: PublicModelVO | null
  candidates: PublicModelVO[]
  /** 未绑定时实际生效的环境变量来源说明 */
  fallback: string | null
}

/** 供用途下拉选择的精简模型信息 */
export interface PublicModelVO {
  id: number
  display_name: string
  model_name: string
  provider: string
  is_default: number
}

export interface ModelTestResult {
  ok: boolean
  latency_ms: number
  message: string
}

export interface AdminUserVO {
  id: number
  tenant_id: number
  /** 登录标识：手机号或工号 */
  login_id: string
  login_type: LoginType
  name: string | null
  nickname: string | null
  job: string | null
  /** 头像地址：/api/files/{key}，未设置时为 null */
  avatar: string | null
  status: string
  /** RBAC 角色编码 */
  roles: string[]
  lock_version: number
  created_at: string
}

export type LoginType = 'PHONE' | 'EMP'

export interface AdminUserListVO {
  total: number
  page: number
  page_size: number
  records: AdminUserVO[]
}

export interface UserCreatePayload {
  login_id: string
  login_type?: LoginType
  password: string
  name?: string | null
  nickname?: string | null
  job?: string | null
  role_codes?: string[]
  tenant_id?: number | null
}

/** 编辑用户：按需传字段，password 留空表示不修改 */
export interface UserUpdatePayload {
  login_id?: string
  name?: string | null
  nickname?: string | null
  job?: string | null
  password?: string
  status?: 'ACTIVE' | 'DISABLED'
  role_codes?: string[]
}

export interface RoleSavePayload {
  code: string
  name: string
  description?: string | null
  permission_codes: string[]
}

/** 角色（内置 + 租户自建） */
export interface RoleVO {
  id: number
  tenant_id: number
  code: string
  name: string
  description: string | null
  is_builtin: boolean
  permission_codes: string[]
}

export interface PermissionVO {
  code: string
  name: string
  module: string
}

export interface TenantVO {
  id: number
  code: string
  name: string
  status: string
  plan: string
  remark: string | null
  created_at: string
}

export interface TenantCreatePayload {
  code: string
  name: string
  plan?: string
  remark?: string | null
  admin_login_id: string
  admin_name?: string | null
  admin_password: string
}

export interface TenantUpdatePayload {
  name?: string
  status?: 'ACTIVE' | 'DISABLED'
  plan?: string
  remark?: string | null
}
