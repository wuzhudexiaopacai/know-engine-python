import { del, get, post, put } from './http'
import type { PageResult } from '@/types/common'
import type {
  AdminUserListVO, AdminUserVO, ModelRoleVO, ModelSavePayload, ModelTestPayload,
  ModelTestResult, ModelVO, PermissionVO, RoleSavePayload, RoleVO, TenantCreatePayload,
  TenantUpdatePayload, TenantVO, UserCreatePayload, UserUpdatePayload,
} from '@/types/admin'

/** 模型配置列表 */
export function listModels(): Promise<ModelVO[]> {
  return get<ModelVO[]>('/admin/models')
}

export function createModel(payload: ModelSavePayload): Promise<ModelVO> {
  return post<ModelVO>('/admin/models', payload)
}

export function updateModel(id: number, payload: ModelSavePayload, lockVersion: number): Promise<ModelVO> {
  return put<ModelVO>(`/admin/models/${id}`, payload, { params: { lock_version: lockVersion } })
}

export function deleteModel(id: number, lockVersion: number): Promise<null> {
  return del<null>(`/admin/models/${id}`, { params: { lock_version: lockVersion } })
}

/** 连通性测试：可测未保存的表单，也可测已保存配置（api_key 留空用库里的） */
export function testModel(payload: ModelTestPayload): Promise<ModelTestResult> {
  return post<ModelTestResult>('/admin/models/test', payload)
}

/** 模型用途列表：每个系统位置的说明、当前绑定与候选模型 */
export function listModelRoles(): Promise<ModelRoleVO[]> {
  return get<ModelRoleVO[]>('/admin/model-roles')
}

/** 绑定/解绑模型用途：modelId 为 null 表示解绑 */
export function bindModelRole(role: string, modelId: number | null): Promise<ModelRoleVO[]> {
  return put<ModelRoleVO[]>(`/admin/model-roles/${role}`, { model_id: modelId })
}

/** 成员列表：平台超管可传 tenant_id 跨租户查询 */
export function listUsers(params: {
  page?: number; size?: number; keyword?: string; tenant_id?: number
}): Promise<AdminUserListVO> {
  return get<AdminUserListVO>('/admin/users', params)
}

/** 角色列表（内置 + 本租户自建） */
export function listRoles(tenantId?: number): Promise<RoleVO[]> {
  return get<RoleVO[]>('/admin/roles', tenantId ? { tenant_id: tenantId } : undefined)
}

export function createRole(payload: RoleSavePayload): Promise<RoleVO> {
  return post<RoleVO>('/admin/roles', payload)
}

export function updateRole(id: number, payload: RoleSavePayload): Promise<RoleVO> {
  return put<RoleVO>(`/admin/roles/${id}`, payload)
}

export function listPermissions(): Promise<PermissionVO[]> {
  return get<PermissionVO[]>('/admin/permissions')
}

/** 租户列表（平台超管） */
export function listTenants(params: {
  page?: number; size?: number; keyword?: string
}): Promise<PageResult<TenantVO>> {
  return get<PageResult<TenantVO>>('/admin/tenants', params)
}

export function createTenant(payload: TenantCreatePayload): Promise<TenantVO> {
  return post<TenantVO>('/admin/tenants', payload)
}

export function updateTenant(id: number, payload: TenantUpdatePayload): Promise<TenantVO> {
  return put<TenantVO>(`/admin/tenants/${id}`, payload)
}

export function createUser(payload: UserCreatePayload): Promise<unknown> {
  return post<unknown>('/admin/users', payload)
}

/** 编辑用户（手机号/姓名/昵称/密码/状态按需） */
export function updateUser(id: number, payload: UserUpdatePayload, lockVersion: number): Promise<AdminUserVO> {
  return put<AdminUserVO>(`/admin/users/${id}`, payload, { params: { lock_version: lockVersion } })
}

/** 逻辑删除用户 */
export function deleteUser(id: number, lockVersion: number): Promise<null> {
  return del<null>(`/admin/users/${id}`, { params: { lock_version: lockVersion } })
}
