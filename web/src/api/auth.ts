import { get, post, put } from './http'
import type {
  UserLoginPayload,
  AdminLoginPayload,
  CaptchaResult,
  LoginResult,
  CurrentUserIdentity,
  ProfileVO,
  ProfileUpdatePayload,
} from '@/types/auth'

/** 获取图形验证码（登录前调用，一次性有效） */
export function fetchCaptcha(): Promise<CaptchaResult> {
  return get<CaptchaResult>('/auth/captcha')
}

/** 用户手机号登录 */
export function loginUser(payload: UserLoginPayload): Promise<LoginResult> {
  return post<LoginResult>('/auth/login', payload)
}

/** 管理员登录（工号） */
export function loginAdmin(payload: AdminLoginPayload): Promise<LoginResult> {
  return post<LoginResult>('/auth/admin-login', payload)
}

/** 查询当前登录身份 */
export function fetchMe(): Promise<CurrentUserIdentity> {
  return get<CurrentUserIdentity>('/auth/me')
}

/** 查询个人资料（头像 / 座右铭等） */
export function fetchProfile(): Promise<ProfileVO> {
  return get<ProfileVO>('/auth/profile')
}

/** 保存个人资料（仅传修改的字段） */
export function updateProfile(payload: ProfileUpdatePayload): Promise<ProfileVO> {
  return put<ProfileVO>('/auth/profile', payload)
}

/** 上传头像图片，返回可长期访问的地址 */
export function uploadAvatar(file: File): Promise<{ url: string }> {
  const form = new FormData()
  form.append('file', file)
  return post<{ url: string }>('/auth/avatar', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}
