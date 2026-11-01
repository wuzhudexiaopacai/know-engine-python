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

/** 用户登录请求 */
export interface UserLoginPayload {
  phone: string
  password: string
  captcha_id: string
  captcha_code: string
}

/** 管理员登录请求 */
export interface AdminLoginPayload {
  emp_id: string
  password: string
  captcha_id: string
  captcha_code: string
}

/** 用户信息 */
export interface UserProfile {
  id: string
  name: string | null
  nickname: string | null
  avatar?: string | null
  motto?: string | null
}

/** 管理员信息 */
export interface AdminUser {
  id: string
  emp_id: string
  name: string | null
  job: string | null
  avatar?: string | null
  motto?: string | null
}

/** 登录响应 */
export interface LoginResult {
  token: string
  identity: Identity
  user: UserProfile | AdminUser
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
