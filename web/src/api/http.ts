import axios, { type AxiosInstance, type AxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'
import type { BaseResult } from '@/types/common'

/** 令牌在本地存储中的键（auth store 与拦截器共用） */
export const TOKEN_KEY = 'know_engine_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

const http: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  timeout: 30000,
})

// 请求拦截：统一注入 Bearer 令牌
http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

/** 登录接口路径（与后端 /api/auth 路由对应） */
const LOGIN_ENDPOINTS = ['/auth/login', '/auth/admin-login']

function isLoginRequest(url?: string): boolean {
  if (!url) return false
  const path = url.split('?')[0]
  return LOGIN_ENDPOINTS.some((endpoint) => path.endsWith(endpoint))
}

// 响应拦截：拆包 BaseResult，业务错误统一提示；401 清令牌并跳登录
// 登录接口例外：账号/密码错误属于「输入不正确」，只提示服务端原因，不能清令牌跳转
http.interceptors.response.use(
  (response) => {
    const body = response.data as BaseResult<unknown>
    // 后端约定 success=false 即业务失败（HTTP 仍可能为 200）
    if (body && typeof body.success === 'boolean' && !body.success) {
      ElMessage.error(body.message || '操作失败')
      if (body.code === 4001 && !isLoginRequest(response.config.url)) {
        handleUnauthorized()
      }
      return Promise.reject(new BizError(body.code, body.message))
    }
    return response
  },
  (error) => {
    // 主动中断（AbortController）不是错误，不弹全局提示，交给调用方处理
    if (axios.isCancel(error)) return Promise.reject(error)
    const status = error?.response?.status
    if (status === 401 && !isLoginRequest(error?.config?.url)) {
      handleUnauthorized()
      ElMessage.error('登录已过期，请重新登录')
    } else {
      const msg = error?.response?.data?.message || error.message || '网络异常，请稍后重试'
      ElMessage.error(msg)
    }
    return Promise.reject(error)
  },
)

/** 业务异常：携带后端 code，供上层按需区分 */
export class BizError extends Error {
  code: number
  constructor(code: number, message: string) {
    super(message)
    this.name = 'BizError'
    this.code = code
  }
}

function handleUnauthorized(): void {
  clearToken()
  // 用 location 兜底跳转，避免在非组件环境引入 router 造成循环依赖
  const target = `/login?redirect=${encodeURIComponent(location.pathname + location.search)}`
  if (!location.pathname.startsWith('/login')) {
    location.assign(target)
  }
}

/** GET 请求，返回已拆包的 data */
export async function get<T>(url: string, params?: Record<string, unknown>, config?: AxiosRequestConfig): Promise<T> {
  const res = await http.get<BaseResult<T>>(url, { params, ...config })
  return res.data.data
}

/** POST 请求（JSON 或 FormData），返回已拆包的 data */
export async function post<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
  const res = await http.post<BaseResult<T>>(url, data, config)
  return res.data.data
}

/** PUT 请求 */
export async function put<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
  const res = await http.put<BaseResult<T>>(url, data, config)
  return res.data.data
}

/** DELETE 请求 */
export async function del<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  const res = await http.delete<BaseResult<T>>(url, config)
  return res.data.data
}

export default http
