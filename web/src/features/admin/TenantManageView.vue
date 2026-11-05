<script setup lang="ts">
// 租户管理（平台超管）：租户开通、套餐与启停；每个租户的数据按 tenant_id 隔离
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { createTenant, listTenants, updateTenant } from '@/api/admin'
import type { TenantCreatePayload, TenantUpdatePayload, TenantVO } from '@/types/admin'
import { formatDateTime } from '@/utils/format'
import { useBreakpoint } from '@/composables/useBreakpoint'

const { isMobile } = useBreakpoint()
const rows = ref<TenantVO[]>([])
const total = ref(0)
const loading = ref(false)
const query = reactive({ page: 1, size: 10, keyword: '' })

const dialogOpen = ref(false)
const saving = ref(false)
/** 编辑中的租户 id；null 表示新建 */
const editingId = ref<number | null>(null)
const form = reactive({
  code: '',
  name: '',
  plan: 'STANDARD',
  remark: '',
  admin_login_id: '',
  admin_name: '',
  admin_password: '',
  status: 'ACTIVE',
})
const dialogTitle = computed(() => (editingId.value ? '编辑租户' : '新建租户'))

async function load(): Promise<void> {
  loading.value = true
  try {
    const res = await listTenants({
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

function resetForm(): void {
  form.code = ''
  form.name = ''
  form.plan = 'STANDARD'
  form.remark = ''
  form.admin_login_id = ''
  form.admin_name = ''
  form.admin_password = ''
  form.status = 'ACTIVE'
}

function openCreate(): void {
  editingId.value = null
  resetForm()
  dialogOpen.value = true
}

function openEdit(row: TenantVO): void {
  editingId.value = row.id
  form.code = row.code
  form.name = row.name
  form.plan = row.plan
  form.remark = row.remark ?? ''
  form.status = row.status
  dialogOpen.value = true
}

async function save(): Promise<void> {
  if (saving.value) return
  const name = form.name.trim()
  if (!name) {
    ElMessage.warning('请输入租户名称')
    return
  }
  saving.value = true
  try {
    if (editingId.value) {
      const payload: TenantUpdatePayload = {
        name,
        plan: form.plan.trim() || 'STANDARD',
        remark: form.remark.trim() || null,
        status: form.status === 'DISABLED' ? 'DISABLED' : 'ACTIVE',
      }
      await updateTenant(editingId.value, payload)
      ElMessage.success('租户已更新')
    } else {
      const code = form.code.trim()
      if (!/^[a-z][a-z0-9_-]{2,31}$/.test(code)) {
        ElMessage.warning('租户编码需为 3-32 位小写字母/数字/-/_，且以字母开头')
        return
      }
      const adminId = form.admin_login_id.trim()
      if (adminId.length < 3) {
        ElMessage.warning('租户管理员工号至少 3 位')
        return
      }
      if (form.admin_password.length < 6) {
        ElMessage.warning('初始密码至少 6 位')
        return
      }
      const payload: TenantCreatePayload = {
        code,
        name,
        plan: form.plan.trim() || 'STANDARD',
        remark: form.remark.trim() || null,
        admin_login_id: adminId,
        admin_name: form.admin_name.trim() || null,
        admin_password: form.admin_password,
      }
      await createTenant(payload)
      ElMessage.success('租户已创建，管理员账号已初始化')
    }
    dialogOpen.value = false
    search()
  } catch {
    /* 拦截器已提示 */
  } finally {
    saving.value = false
  }
}

/** 停用后该租户账号无法登录，数据保留 */
async function toggleStatus(row: TenantVO): Promise<void> {
  const next = row.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE'
  try {
    await updateTenant(row.id, { status: next })
    ElMessage.success(next === 'DISABLED' ? '租户已停用' : '租户已启用')
    await load()
  } catch {
    /* 拦截器已提示 */
  }
}

onMounted(() => void load())
</script>

<template>
  <div class="ke-page tenants">
    <header class="tenants__head">
      <div>
        <h1 class="tenants__title">租户管理</h1>
        <p class="tenants__sub ke-muted">开通租户后，各租户以独立编码登录，成员与知识库数据按租户隔离</p>
      </div>
      <el-button type="primary" :icon="'Plus'" @click="openCreate">新建租户</el-button>
    </header>

    <section class="tenants__toolbar ke-card" aria-label="筛选">
      <el-input v-model="query.keyword" placeholder="按租户编码 / 名称搜索" clearable class="tenants__kw"
                @keyup.enter="search" @clear="search" aria-label="搜索租户">
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <el-button @click="search">查询</el-button>
    </section>

    <section class="tenants__table ke-card ke-card--table" aria-label="租户列表">
      <!-- H5：宽表格改为信息卡片 -->
      <div v-if="isMobile" v-loading="loading" class="tenants__cards">
        <el-empty v-if="!rows.length" description="暂无租户，点击上方新建" />
        <ul v-else class="ke-mlist">
          <li v-for="row in rows" :key="row.id" class="ke-mcard">
            <div class="ke-mcard__head">
              <div class="tenants__card-name">
                <span class="ke-mcard__title">{{ row.name }}</span>
                <span class="tenants__code">{{ row.code }}</span>
              </div>
              <el-tag size="small" :type="row.status === 'ACTIVE' ? 'success' : 'info'" effect="light">
                {{ row.status === 'ACTIVE' ? '已开通' : '已停用' }}
              </el-tag>
            </div>
            <div class="ke-mfacts">
              <div class="ke-mfact">
                <span class="ke-mfact__label">套餐</span>
                <span class="ke-mfact__value">{{ row.plan || 'STANDARD' }}</span>
              </div>
              <div class="ke-mfact">
                <span class="ke-mfact__label">创建时间</span>
                <span class="ke-mfact__value">{{ formatDateTime(row.created_at) }}</span>
              </div>
              <div v-if="row.remark" class="ke-mfact">
                <span class="ke-mfact__label">备注</span>
                <span class="ke-mfact__value">{{ row.remark }}</span>
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
                {{ row.status === 'ACTIVE' ? '停用' : '启用' }}
              </el-button>
            </div>
          </li>
        </ul>
      </div>

      <el-table v-else v-loading="loading" :data="rows" stripe row-key="id">
        <template #empty>
          <el-empty description="暂无租户，点击右上角新建" />
        </template>

        <el-table-column label="租户名称" min-width="150">
          <template #default="{ row }">{{ row.name }}</template>
        </el-table-column>

        <el-table-column label="租户编码" min-width="130">
          <template #default="{ row }">
            <span class="tenants__code">{{ row.code }}</span>
          </template>
        </el-table-column>

        <el-table-column label="套餐" width="120">
          <template #default="{ row }">{{ row.plan || 'STANDARD' }}</template>
        </el-table-column>

        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'ACTIVE' ? 'success' : 'info'" effect="light">
              {{ row.status === 'ACTIVE' ? '已开通' : '已停用' }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="备注" min-width="160">
          <template #default="{ row }"><span class="ke-muted">{{ row.remark || '—' }}</span></template>
        </el-table-column>

        <el-table-column label="创建时间" width="160">
          <template #default="{ row }"><span class="ke-muted">{{ formatDateTime(row.created_at) }}</span></template>
        </el-table-column>

        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row as TenantVO)">编辑</el-button>
            <el-button
              link
              :type="row.status === 'ACTIVE' ? 'warning' : 'success'"
              size="small"
              @click="toggleStatus(row as TenantVO)"
            >
              {{ row.status === 'ACTIVE' ? '停用' : '启用' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="tenants__pager">
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
        <el-form-item label="租户编码" required>
          <el-input v-model="form.code" :disabled="!!editingId" maxlength="32"
                    placeholder="小写字母开头，3-32 位，登录时输入" />
        </el-form-item>
        <el-form-item label="租户名称" required>
          <el-input v-model="form.name" maxlength="128" placeholder="如 XX 科技有限公司" />
        </el-form-item>
        <el-form-item label="套餐">
          <el-input v-model="form.plan" maxlength="32" placeholder="STANDARD / PREMIUM" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.remark" maxlength="255" placeholder="可选" />
        </el-form-item>
        <template v-if="!editingId">
          <el-divider content-position="left">租户管理员</el-divider>
          <el-form-item label="管理员工号" required>
            <el-input v-model="form.admin_login_id" maxlength="64" placeholder="至少 3 位，用于管理员登录" />
          </el-form-item>
          <el-form-item label="管理员姓名">
            <el-input v-model="form.admin_name" maxlength="64" placeholder="可选" />
          </el-form-item>
          <el-form-item label="初始密码" required>
            <el-input v-model="form.admin_password" type="password" show-password maxlength="64"
                      placeholder="至少 6 位" />
          </el-form-item>
        </template>
        <el-form-item v-else label="状态">
          <el-radio-group v-model="form.status">
            <el-radio-button value="ACTIVE">开通</el-radio-button>
            <el-radio-button value="DISABLED">停用</el-radio-button>
          </el-radio-group>
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
.tenants {
  &__head { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: var(--ke-space-5); }
  &__title { margin: 0; font-size: 20px; font-weight: 600; letter-spacing: -0.01em; color: var(--ke-gray-900); }
  &__sub { margin: 4px 0 0; font-size: 13px; }

  &__toolbar {
    display: flex; align-items: center; gap: var(--ke-space-3);
    padding: var(--ke-space-4); margin-bottom: var(--ke-space-4);
  }
  &__kw { width: 260px; }

  &__table { padding: 0; }
  &__code { font-family: var(--ke-font-mono, monospace); font-size: 13px; color: var(--ke-brand-600); }
  &__pager { display: flex; justify-content: flex-end; padding: var(--ke-space-4); }
  &__card-name { display: flex; flex-direction: column; gap: 2px; min-width: 0; }

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
