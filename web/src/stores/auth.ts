import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { loginUser, loginAdmin, fetchMe } from '@/api/auth'
import { setToken, clearToken, getToken } from '@/api/http'
import type { Identity } from '@/types/common'
import type {
  UserProfile, AdminUser, UserLoginPayload, AdminLoginPayload, ProfileVO,
} from '@/types/auth'

/**
 * 全局认证态：令牌、身份、展示名。
 * 令牌持久化在 localStorage，刷新后由 loadMe() 恢复身份；跨模块共享，故入 store。
 */
export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(getToken())
  const identity = ref<Identity | null>(null)
  /** 当前账号主键（用户/管理员各自表内自增 id，登录响应与 /auth/me 的 sub） */
  const sub = ref<string | null>(null)
  const user = ref<UserProfile | AdminUser | null>(null)
  const displayName = ref<string>('')
  /** 个人资料：头像地址与座右铭（顶栏与资料弹窗共用） */
  const avatar = ref<string | null>(null)
  const motto = ref<string | null>(null)

  const isAuthenticated = computed(() => !!token.value)
  const isAdmin = computed(() => identity.value === 'admin')
  /** 与会话归属键保持一致（后端 owner_key = identity:sub），用于判定会话是否本人所有 */
  const ownerKey = computed(() =>
    identity.value && sub.value ? `${identity.value}:${sub.value}` : null)

  function applyLogin(tk: string, idt: Identity, u: UserProfile | AdminUser): void {
    token.value = tk
    identity.value = idt
    sub.value = u.id || null
    user.value = u
    avatar.value = u.avatar ?? null
    motto.value = u.motto ?? null
    const name = (u as AdminUser).name || (u as UserProfile).nickname || (u as UserProfile).name
    displayName.value = name || (idt === 'admin' ? '管理员' : '用户')
    setToken(tk)
  }

  async function doLoginUser(payload: UserLoginPayload): Promise<void> {
    const res = await loginUser(payload)
    applyLogin(res.token, res.identity, res.user)
  }

  async function doLoginAdmin(payload: AdminLoginPayload): Promise<void> {
    const res = await loginAdmin(payload)
    applyLogin(res.token, res.identity, res.user)
  }

  /** 应用启动或刷新后：凭本地令牌拉取身份，恢复登录态；失败则清空 */
  async function loadMe(): Promise<void> {
    if (!token.value) return
    try {
      const me = await fetchMe()
      identity.value = me.identity
      sub.value = me.sub || null
      avatar.value = me.avatar ?? null
      motto.value = me.motto ?? null
      const name = me.identity === 'admin' ? me.name : (me.nickname || me.name)
      displayName.value = name || (me.identity === 'admin' ? '管理员' : '用户')
    } catch {
      logout()
    }
  }

  /** 资料保存后同步全局展示态（头像、座右铭、展示名） */
  function applyProfile(p: ProfileVO): void {
    avatar.value = p.avatar
    motto.value = p.motto
    const name = p.identity === 'admin' ? p.name : (p.nickname || p.name)
    if (name) displayName.value = name
  }

  function logout(): void {
    token.value = null
    identity.value = null
    sub.value = null
    user.value = null
    displayName.value = ''
    avatar.value = null
    motto.value = null
    clearToken()
  }

  return {
    token, identity, sub, user, displayName, avatar, motto,
    isAuthenticated, isAdmin, ownerKey,
    doLoginUser, doLoginAdmin, loadMe, applyProfile, logout,
  }
})
