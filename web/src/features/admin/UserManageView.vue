<script setup lang="ts">
// 成员管理：统一账号 + RBAC 角色分配
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { createUser, deleteUser, listRoles, listUsers, updateUser } from '@/api/admin'
import type { AdminUserVO, RoleVO, UserCreatePayload, UserUpdatePayload } from '@/types/admin'
import { formatDateTime } from '@/utils/format'
import { useBreakpoint } from '@/composables/useBreakpoint'

const { isMobile } = useBreakpoint()
const rows = ref<AdminUserVO[]>([])
const roles = ref<RoleVO[]>([])
const total = ref(0)
const loading = ref(false)
const query = reactive({ page: 1, size: 10, keyword: '' })

const dialogOpen = ref(false)
const saving = ref(false)
/** 编辑中的用户 id；null 表示新增 */
const editingId = ref<number | null>(null)
const lockVersion = ref(0)
const form = reactive<UserCreatePayload>({
  login_id: '', password: '', name: '', nickname: '', login_type: 'PHONE', role_codes: ['member'],
})
const dialogTitle = computed(() => (editingId.value ? '编辑成员' : '新增成员'))

/** 无头像时的占位文字：优先姓名/昵称首字，否则取登录标识末两位 */
function avatarText(row: AdminUserVO): string {
  const label = (row.name || row.nickname || '').trim()
  if (label) return label.slice(0, 1).toUpperCase()
  return (row.login_id || '?').slice(-2)
}

/** 角色展示名：内置角色显示中文名，未知角色回退编码 */
function roleLabel(code: string): string {
  return roles.value.find((r) => r.code === code)?.name ?? code
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const res = await listUsers({
      page: query.page, size: query.size, keyword: query.keyword.trim() || undefined,
    })
    rows.value = res.records
    total.value = res.total
  } catch {
    /* 拦截器已提示 */
  } finally {
    loading.value = false
  }
}

function search(): void {
  query.page = 1
  void load()
}

function openCreate(): void {
  editingId.value = null
  lockVersion.value = 0
  form.login_id = ''
  form.password = ''
  form.name = ''
  form.nickname = ''
  form.job = ''
  form.login_type = 'PHONE'
  form.role_codes = ['member']
  dialogOpen.value = true
}

function openEdit(row: AdminUserVO): void {
  editingId.value = row.id
  lockVersion.value = row.lock_version
  form.login_id = row.login_id
  form.password = ''
  form.name = row.name ?? ''
  form.nickname = row.nickname ?? ''
  form.job = row.job ?? ''
  form.login_type = row.login_type
  form.role_codes = [...(row.roles ?? ['member'])]
  dialogOpen.value = true
}

async function save(): Promise<void> {
  if (saving.value) return
  const loginId = form.login_id.trim()
  if (form.login_type === 'PHONE' && !/^1\d{10}$/.test(loginId)) {
    ElMessage.warning('请输入 11 位手机号')
    return
  }
  if (form.login_type === 'EMP' && loginId.length < 3) {
    ElMessage.warning('工号至少 3 位')
    return
  }
  const password = form.password?.trim() ?? ''
  // 新增必填密码；编辑留空表示沿用原密码
  if (!editingId.value && password.length < 6) {
    ElMessage.warning('密码至少 6 位')
    return
  }
  if (editingId.value && password && password.length < 6) {
    ElMessage.warning('新密码至少 6 位')
    return
  }
  saving.value = true
  try {
    if (editingId.value) {
      const payload: UserUpdatePayload = {
        login_id: loginId,
        name: form.name?.trim() || null,
        nickname: form.nickname?.trim() || null,
        job: form.job?.trim() || null,
        role_codes: form.role_codes,
      }
      if (password) payload.password = password
      await updateUser(editingId.value, payload, lockVersion.value)
      ElMessage.success('成员已更新')
    } else {
      await createUser({
        login_id: loginId,
        login_type: form.login_type,
        password,
        name: form.name?.trim() || null,
        nickname: form.nickname?.trim() || null,
        job: form.job?.trim() || null,
        role_codes: form.role_codes,
      })
      ElMessage.success('成员已创建')
    }
    dialogOpen.value = false
    search()
  } catch {
    /* 拦截器已提示 */
  } finally {
    saving.value = false
  }
}

/** 禁用/启用：普通用户被禁用后无法登录，已登录会话自然失效 */
async function toggleStatus(row: AdminUserVO): Promise<void> {
  const next = row.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE'
  try {
    await updateUser(row.id, { status: next }, row.lock_version)
    ElMessage.success(next === 'DISABLED' ? '用户已禁用' : '用户已启用')
    await load()
  } catch {
    /* 拦截器已提示 */
  }
}

async function remove(row: AdminUserVO): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定删除成员「${row.name || row.nickname || row.login_id}」吗？删除后该账号无法登录，历史会话仍保留。`,
      '删除用户', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deleteUser(row.id, row.lock_version)
    ElMessage.success('用户已删除')
    await load()
  } catch {
    /* 拦截器已提示 */
  }
}

async function loadRoles(): Promise<void> {
  try {
    roles.value = await listRoles()
  } catch {
    /* 拦截器已提示 */
  }
}

onMounted(() => {
  void loadRoles()
  void load()
})
</script>

<template>
  <div class="ke-page users">
    <header class="users__head">
      <div>
        <h1 class="users__title">成员管理</h1>
        <p class="users__sub ke-muted">统一账号（手机号或工号登录），按角色授予权限，数据按租户隔离</p>
      </div>
      <el-button type="primary" :icon="'Plus'" @click="openCreate">新增成员</el-button>
    </header>

    <section class="users__toolbar ke-card" aria-label="筛选">
      <el-input v-model="query.keyword" placeholder="按登录标识 / 姓名搜索" clearable class="users__kw"
                @keyup.enter="search" @clear="search" aria-label="搜索关键字">
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <el-button @click="search">查询</el-button>
    </section>

    <section class="users__table ke-card ke-card--table" aria-label="成员列表">
      <!-- H5：宽表格改为信息卡片 -->
      <div v-if="isMobile" v-loading="loading" class="users__cards">
        <el-empty v-if="!rows.length" description="暂无成员，点击上方新增" />
        <ul v-else class="ke-mlist">
          <li v-for="row in rows" :key="row.id" class="ke-mcard">
            <div class="ke-mcard__head">
              <div class="users__card-id">
                <el-avatar :size="36" :src="row.avatar || undefined" class="users__avatar">
                  {{ avatarText(row) }}
                </el-avatar>
                <div class="users__card-name">
                  <span class="ke-mcard__title">{{ row.name || row.nickname || row.login_id }}</span>
                  <span v-if="row.name || row.nickname" class="users__card-phone">{{ row.login_id }}</span>
                </div>
              </div>
              <el-tag size="small" :type="row.status === 'ACTIVE' ? 'success' : 'info'" effect="light">
                {{ row.status === 'ACTIVE' ? '已启用' : row.status === 'DISABLED' ? '已停用' : row.status }}
              </el-tag>
            </div>
            <div class="ke-mfacts">
              <div class="ke-mfact">
                <span class="ke-mfact__label">角色</span>
                <span class="ke-mfact__value">{{ row.roles.map(roleLabel).join('、') || '—' }}</span>
              </div>
              <div class="ke-mfact">
                <span class="ke-mfact__label">创建时间</span>
                <span class="ke-mfact__value">{{ formatDateTime(row.created_at) }}</span>
              </div>
            </div>
            <div class="ke-mcard__actions">
              <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
              <el-button
                link
                :type="row.status === 'ACTIVE' ? 'warning' : 'success'"
                size="small"
                @click="toggleStatus(row)"
              >
                {{ row.status === 'ACTIVE' ? '禁用' : '启用' }}
              </el-button>
              <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
            </div>
          </li>
        </ul>
      </div>

      <el-table v-else v-loading="loading" :data="rows" stripe row-key="id">
        <template #empty>
          <el-empty description="暂无成员，点击右上角新增" />
        </template>

        <el-table-column label="头像" width="80" align="center">
          <template #default="{ row }">
            <el-avatar :size="32" :src="row.avatar || undefined" class="users__avatar">
              {{ avatarText(row as AdminUserVO) }}
            </el-avatar>
          </template>
        </el-table-column>

        <el-table-column label="登录标识" min-width="150">
          <template #default="{ row }">
            <span>{{ row.login_id }}</span>
            <el-tag size="small" effect="plain" class="users__type">
              {{ row.login_type === 'EMP' ? '工号' : '手机号' }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="姓名" min-width="110">
          <template #default="{ row }">{{ row.name || '—' }}</template>
        </el-table-column>

        <el-table-column label="角色" min-width="180">
          <template #default="{ row }">
            <el-tag v-for="code in row.roles" :key="code" size="small" effect="light" class="users__role">
              {{ roleLabel(code) }}
            </el-tag>
            <span v-if="!row.roles.length" class="ke-muted">未分配</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'ACTIVE' ? 'success' : 'info'" effect="light">
              {{ row.status === 'ACTIVE' ? '已启用' : row.status === 'DISABLED' ? '已停用' : row.status }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="创建时间" width="160">
          <template #default="{ row }"><span class="ke-muted">{{ formatDateTime(row.created_at) }}</span></template>
        </el-table-column>

        <el-table-column label="操作" width="190" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row as AdminUserVO)">编辑</el-button>
            <el-button
              link
              :type="row.status === 'ACTIVE' ? 'warning' : 'success'"
              size="small"
              @click="toggleStatus(row as AdminUserVO)"
            >
              {{ row.status === 'ACTIVE' ? '禁用' : '启用' }}
            </el-button>
            <el-button link type="danger" size="small" @click="remove(row as AdminUserVO)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="users__pager">
        <el-pagination
          v-model:current-page="query.page"
          v-model:page-size="query.size"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="load"
          @size-change="search"
        />
      </div>
    </section>

    <el-dialog v-model="dialogOpen" :title="dialogTitle" width="min(520px, 94vw)" append-to-body>
      <el-form :model="form" label-position="top">
        <el-form-item label="登录方式" required>
          <el-radio-group v-model="form.login_type" :disabled="!!editingId">
            <el-radio-button value="PHONE">手机号</el-radio-button>
            <el-radio-button value="EMP">工号</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="form.login_type === 'EMP' ? '工号' : '手机号'" required>
          <el-input
            v-model="form.login_id"
            :maxlength="form.login_type === 'EMP' ? 64 : 11"
            :inputmode="form.login_type === 'PHONE' ? 'numeric' : 'text'"
            :placeholder="form.login_type === 'EMP' ? '登录工号' : '11 位手机号，登录凭证'"
          />
        </el-form-item>
        <el-form-item :label="editingId ? '重置密码' : '密码'" :required="!editingId">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            maxlength="64"
            :placeholder="editingId ? '留空表示不修改密码' : '至少 6 位'"
          />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role_codes" multiple placeholder="选择角色" class="users__roles">
            <el-option v-for="r in roles" :key="r.code" :label="r.name" :value="r.code">
              <span>{{ r.name }}</span>
              <span class="users__role-code">{{ r.code }}</span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="form.name" maxlength="64" placeholder="可选" />
        </el-form-item>
        <el-form-item label="昵称">
          <el-input v-model="form.nickname" maxlength="128" placeholder="可选，显示在右上角" />
        </el-form-item>
        <el-form-item label="岗位">
          <el-input v-model="form.job" maxlength="128" placeholder="可选，如知识库管理员" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="saving" @click="dialogOpen = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{ editingId ? '保存' : '创建' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.users {
  &__head { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: var(--ke-space-5); }
  &__title { margin: 0; font-size: 20px; font-weight: 600; letter-spacing: -0.01em; color: var(--ke-gray-900); }
  &__sub { margin: 4px 0 0; font-size: 13px; }

  &__toolbar {
    display: flex; align-items: center; gap: var(--ke-space-3);
    padding: var(--ke-space-4); margin-bottom: var(--ke-space-4);
  }
  &__kw { width: 260px; }

  &__table { padding: 0; }
  &__avatar { color: var(--ke-brand-600); background: var(--ke-brand-50); font-size: 13px; font-weight: 600; }
  &__pager { display: flex; justify-content: flex-end; padding: var(--ke-space-4); }
  &__card-id { display: flex; align-items: center; gap: 10px; min-width: 0; }
  &__card-name { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  &__card-phone { font-size: 12px; color: var(--ke-gray-400); }
  &__role { margin-right: 6px; }
  &__type { margin-left: 8px; }
  &__roles { width: 100%; }
  &__role-code { float: right; color: var(--ke-gray-400); font-size: 12px; }

  /* ---------- H5：筛选铺满、列表卡片化 ---------- */
  @media (max-width: 768px) {
    &__head { align-items: center; margin-bottom: 12px; gap: 8px; }
    &__title { font-size: 18px; }
    &__sub { font-size: 12px; line-height: 1.6; }

    &__toolbar { flex-wrap: wrap; gap: 8px; padding: 10px; margin-bottom: 10px; }
    &__kw { width: 100%; }
    &__toolbar .el-button { width: 100%; margin-left: 0; }

    &__cards { padding: 12px; }
    &__pager { padding: 12px 0 0; justify-content: center; }
  }
}
</style>
