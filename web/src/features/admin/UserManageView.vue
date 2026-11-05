<script setup lang="ts">
// 普通用户管理：管理员新增手机号账号，普通用户仅能对话
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { createUser, deleteUser, listUsers, updateUser } from '@/api/admin'
import type { AdminUserVO, UserCreatePayload, UserUpdatePayload } from '@/types/admin'
import { formatDateTime } from '@/utils/format'
import { useBreakpoint } from '@/composables/useBreakpoint'

const { isMobile } = useBreakpoint()
const rows = ref<AdminUserVO[]>([])
const total = ref(0)
const loading = ref(false)
const query = reactive({ page: 1, size: 10, keyword: '' })

const dialogOpen = ref(false)
const saving = ref(false)
/** 编辑中的用户 id；null 表示新增 */
const editingId = ref<number | null>(null)
const lockVersion = ref(0)
const form = reactive<UserCreatePayload>({ phone: '', password: '', name: '', nickname: '' })
const dialogTitle = computed(() => (editingId.value ? '编辑用户' : '新增普通用户'))

/** 无头像时的占位文字：优先姓名/昵称首字，否则取手机号末两位 */
function avatarText(row: AdminUserVO): string {
  const label = (row.name || row.nickname || '').trim()
  if (label) return label.slice(0, 1).toUpperCase()
  return (row.phone || '?').slice(-2)
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
  form.phone = ''
  form.password = ''
  form.name = ''
  form.nickname = ''
  dialogOpen.value = true
}

function openEdit(row: AdminUserVO): void {
  editingId.value = row.id
  lockVersion.value = row.lock_version
  form.phone = row.phone
  form.password = ''
  form.name = row.name ?? ''
  form.nickname = row.nickname ?? ''
  dialogOpen.value = true
}

async function save(): Promise<void> {
  if (saving.value) return
  if (!/^1\d{10}$/.test(form.phone.trim())) {
    ElMessage.warning('请输入 11 位手机号')
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
        phone: form.phone.trim(),
        name: form.name?.trim() || null,
        nickname: form.nickname?.trim() || null,
      }
      if (password) payload.password = password
      await updateUser(editingId.value, payload, lockVersion.value)
      ElMessage.success('用户已更新')
    } else {
      await createUser({
        phone: form.phone.trim(),
        password,
        name: form.name?.trim() || null,
        nickname: form.nickname?.trim() || null,
      })
      ElMessage.success('用户已创建')
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
      `确定删除用户「${row.name || row.nickname || row.phone}」吗？删除后该账号无法登录，历史会话仍保留。`,
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

onMounted(load)
</script>

<template>
  <div class="ke-page users">
    <header class="users__head">
      <div>
        <h1 class="users__title">用户管理</h1>
        <p class="users__sub ke-muted">新增普通用户账号（手机号登录，仅可对话，会话数据按人隔离）</p>
      </div>
      <el-button type="primary" :icon="'Plus'" @click="openCreate">新增用户</el-button>
    </header>

    <section class="users__toolbar ke-card" aria-label="筛选">
      <el-input v-model="query.keyword" placeholder="按手机号 / 姓名搜索" clearable class="users__kw"
                @keyup.enter="search" @clear="search" aria-label="搜索关键字">
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <el-button @click="search">查询</el-button>
    </section>

    <section class="users__table ke-card ke-card--table" aria-label="用户列表">
      <!-- H5：宽表格改为信息卡片 -->
      <div v-if="isMobile" v-loading="loading" class="users__cards">
        <el-empty v-if="!rows.length" description="暂无普通用户，点击上方新增" />
        <ul v-else class="ke-mlist">
          <li v-for="row in rows" :key="row.id" class="ke-mcard">
            <div class="ke-mcard__head">
              <div class="users__card-id">
                <el-avatar :size="36" :src="row.avatar || undefined" class="users__avatar">
                  {{ avatarText(row) }}
                </el-avatar>
                <div class="users__card-name">
                  <span class="ke-mcard__title">{{ row.name || row.nickname || row.phone }}</span>
                  <span v-if="row.name || row.nickname" class="users__card-phone">{{ row.phone }}</span>
                </div>
              </div>
              <el-tag size="small" :type="row.status === 'ACTIVE' ? 'success' : 'info'" effect="light">
                {{ row.status === 'ACTIVE' ? '已启用' : row.status === 'DISABLED' ? '已停用' : row.status }}
              </el-tag>
            </div>
            <div class="ke-mfacts">
              <div class="ke-mfact">
                <span class="ke-mfact__label">昵称</span>
                <span class="ke-mfact__value">{{ row.nickname || '—' }}</span>
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
          <el-empty description="暂无普通用户，点击右上角新增" />
        </template>

        <el-table-column label="头像" width="80" align="center">
          <template #default="{ row }">
            <el-avatar :size="32" :src="row.avatar || undefined" class="users__avatar">
              {{ avatarText(row as AdminUserVO) }}
            </el-avatar>
          </template>
        </el-table-column>

        <el-table-column label="手机号" min-width="140" prop="phone" />

        <el-table-column label="姓名" min-width="120">
          <template #default="{ row }">{{ row.name || '—' }}</template>
        </el-table-column>

        <el-table-column label="昵称" min-width="140">
          <template #default="{ row }">{{ row.nickname || '—' }}</template>
        </el-table-column>

        <el-table-column label="状态" width="110">
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

    <el-dialog v-model="dialogOpen" :title="dialogTitle" width="min(480px, 94vw)" append-to-body>
      <el-form :model="form" label-position="top">
        <el-form-item label="手机号" required>
          <el-input v-model="form.phone" maxlength="11" inputmode="numeric" placeholder="11 位手机号，登录凭证" />
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
        <el-form-item label="姓名">
          <el-input v-model="form.name" maxlength="64" placeholder="可选" />
        </el-form-item>
        <el-form-item label="昵称">
          <el-input v-model="form.nickname" maxlength="128" placeholder="可选，显示在右上角" />
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
