import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { loginUser, loginAdmin, fetchMe } from '@/api/auth'
import { setToken, clearToken, getToken } from '@/api/http'
import type { Identity } from '@/types/common'
import type {
  LoginAccount, LoginResult, TenantBrief, UserLoginPayload, AdminLoginPayload, ProfileVO,
} from '@/types/auth'

/** 管理员身份角色：与后端 ADMIN_ROLE_CODES 保持一致（用于「姓名 / 昵称」等展示差异） */
const ADMIN_ROLE_CODES = ['platform_admin', 'tenant_admin', 'kb_admin', 'reviewer']
/** 平台超管：跨租户视角 */
const PLATFORM_ADMIN_CODE = 'platform_admin'
/** 通配权限：平台超管持有全部权限 */
const ALL = '*'

/**
 * 全局认证态：令牌、租户、角色权限与展示名。
 * 令牌持久化在 localStorage，刷新后由 loadMe() 恢复身份；跨模块共享，故入 store。
 */
export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(getToken())
  const identity = ref<Identity | null>(null)
  /** 当前账号主键（统一账号表自增 id，登录响应与 /auth/me 的 sub） */
  const sub = ref<string | null>(null)
  const user = ref<LoginAccount | null>(null)
  const displayName = ref<string>('')
  /** 个人资料：头像地址与座右铭（顶栏与资料弹窗共用） */
  const avatar = ref<string | null>(null)
  const motto = ref<string | null>(null)

  /** 租户上下文：平台超管的 tenant 为其自身平台租户，数据视角跨租户 */
  const tenant = ref<TenantBrief | null>(null)
  const roles = ref<string[]>([])
  const permissions = ref<string[]>([])

  const isAuthenticated = computed(() => !!token.value)
  const isAdmin = computed(() => roles.value.some((r) => ADMIN_ROLE_CODES.includes(r)))
  const isPlatformAdmin = computed(() => roles.value.includes(PLATFORM_ADMIN_CODE))
  const tenantId = computed(() => tenant.value?.id ?? null)
  const tenantCode = computed(() => tenant.value?.code ?? '')
  /** 与会话归属键保持一致（后端 owner_key = identity:sub），用于判定会话是否本人所有 */
  const ownerKey = computed(() =>
    identity.value && sub.value ? `${identity.value}:${sub.value}` : null)

  /** 权限判定：平台超管通配 `*` 视为拥有全部权限 */
  function can(permission: string): boolean {
    return permissions.value.includes(ALL) || permissions.value.includes(permission)
  }

  /** 满足任意一个权限即通过（与后端 require_permission 语义一致） */
  function canAny(...items: string[]): boolean {
    return items.some((p) => can(p))
  }

  function applyAuth(res: LoginResult): void {
    token.value = res.token
    identity.value = res.identity
    sub.value = res.user.id || null
    user.value = res.user
    tenant.value = res.tenant ?? null
    roles.value = res.roles ?? []
    permissions.value = res.permissions ?? []
    avatar.value = res.user.avatar ?? null
    motto.value = res.user.motto ?? null
    displayName.value = res.user.nickname || res.user.name || res.user.login_id
      || (res.identity === 'admin' ? '管理员' : '用户')
    setToken(res.token)
  }

  async function doLoginUser(payload: UserLoginPayload): Promise<void> {
    applyAuth(await loginUser(payload))
  }

  async function doLoginAdmin(payload: AdminLoginPayload): Promise<void> {
    applyAuth(await loginAdmin(payload))
  }

  /** 应用启动或刷新后：凭本地令牌拉取身份，恢复登录态；失败则清空 */
  async function loadMe(): Promise<void> {
    if (!token.value) return
    try {
      const me = await fetchMe()
      identity.value = me.identity
      sub.value = me.sub || null
      tenant.value = me.tenant ?? null
      roles.value = me.roles ?? []
      permissions.value = me.permissions ?? []
      avatar.value = me.avatar ?? null
      motto.value = me.motto ?? null
      displayName.value = me.nickname || me.name
        || (me.identity === 'admin' ? '管理员' : '用户')
    } catch {
      logout()
    }
  }

  /** 资料保存后同步全局展示态（头像、座右铭、展示名） */
  function applyProfile(p: ProfileVO): void {
    avatar.value = p.avatar
    motto.value = p.motto
    const name = p.nickname || p.name
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
    tenant.value = null
    roles.value = []
    permissions.value = []
    clearToken()
  }

  return {
    token, identity, sub, user, displayName, avatar, motto,
    tenant, tenantId, tenantCode, roles, permissions,
    isAuthenticated, isAdmin, isPlatformAdmin, ownerKey,
    can, canAny,
    doLoginUser, doLoginAdmin, loadMe, applyProfile, logout,
  }
})
