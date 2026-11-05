import type { Identity } from './common'

/** 图形验证码（/api/auth/captcha） */
export interface CaptchaResult {
  /** 一次性标识，提交登录时回传给服务端 */
  captcha_id: string
  /** 图片内容（base64 data URL，可直接作为 img src） */
  image: string
  /** 有效期（秒） */
  expires_in: number
}

/** 登录请求公共字段：租户编码用于 SaaS 多租户路由 */
interface LoginBasePayload {
  tenant_code: string
  password: string
  captcha_id: string
  captcha_code: string
}

/** 用户登录请求（手机号） */
export interface UserLoginPayload extends LoginBasePayload {
  phone: string
}

/** 管理员登录请求（工号） */
export interface AdminLoginPayload extends LoginBasePayload {
  emp_id: string
}

/** 租户摘要 */
export interface TenantBrief {
  id: number
  code: string
  name?: string | null
}

/** 登录返回的账号信息（统一账号表） */
export interface LoginAccount {
  id: string
  /** 登录标识：手机号或工号 */
  login_id: string
  name: string | null
  nickname: string | null
  job: string | null
  avatar?: string | null
  motto?: string | null
}

/** 登录响应 */
export interface LoginResult {
  token: string
  identity: Identity
  tenant: TenantBrief | null
  /** RBAC 角色编码 */
  roles: string[]
  /** RBAC 权限点；平台超管为通配 ["*"] */
  permissions: string[]
  user: LoginAccount
}

/** 当前身份（/api/auth/me） */
export interface CurrentUserIdentity {
  sub: string
  identity: Identity
  name: string | null
  nickname?: string | null
  avatar?: string | null
  motto?: string | null
  lock_version?: number
  tenant?: TenantBrief | null
  roles: string[]
  permissions: string[]
}

/** 个人资料（/api/auth/profile） */
export interface ProfileVO {
  id: number
  identity: Identity
  name: string | null
  nickname: string | null
  avatar: string | null
  motto: string | null
  lock_version: number
}

/** 个人资料更新（按需传字段） */
export interface ProfileUpdatePayload {
  name?: string | null
  nickname?: string | null
  motto?: string | null
  avatar?: string | null
  lock_version?: number
}
