<script setup lang="ts">
// 问答缓存审核：按状态筛选，审核通过/驳回/下线，展示问题、答案与统计
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listEntries, approveEntry, rejectEntry, disableEntry } from '@/api/qaCache'
import type { QaCacheEntry } from '@/types/qaCache'
import type { CacheStatus } from '@/types/common'
import { CACHE_STATUS_LABEL, CACHE_STATUS_TYPE, formatDateTime } from '@/utils/format'

const rows = ref<QaCacheEntry[]>([])
const total = ref(0)
const loading = ref(false)
const query = reactive({ page: 1, size: 20, status: '' as '' | CacheStatus })

const statusOptions = computed(() =>
  Object.entries(CACHE_STATUS_LABEL).map(([value, label]) => ({ value, label })))

async function load(): Promise<void> {
  loading.value = true
  try {
    const res = await listEntries({ page: query.page, size: query.size, status: query.status || undefined })
    rows.value = res.records
    total.value = res.total
  } catch {
    /* 拦截器已提示 */
  } finally {
    loading.value = false
  }
}

function search(): void { query.page = 1; void load() }

/** 取回可选备注；取消返回 symbol 表示放弃操作 */
async function promptNote(title: string, tip: string): Promise<string | null | 'cancel'> {
  try {
    const { value } = await ElMessageBox.prompt(tip, title, {
      inputType: 'textarea', inputValidator: () => true, confirmButtonText: '确定', cancelButtonText: '取消',
    })
    return value || ''
  } catch {
    return 'cancel'
  }
}

async function approve(e: QaCacheEntry): Promise<void> {
  const note = await promptNote('通过并上线', '可填写审核备注（可选）')
  if (note === 'cancel') return
  try {
    await approveEntry(e.id, note || undefined)
    ElMessage.success('已通过并上线')
    void load()
  } catch { /* 拦截器已提示 */ }
}

async function reject(e: QaCacheEntry): Promise<void> {
  const note = await promptNote('驳回', '请填写驳回理由')
  if (note === 'cancel') return
  if (!note) { ElMessage.warning('驳回需填写理由'); return }
  try {
    await rejectEntry(e.id, note)
    ElMessage.success('已驳回')
    void load()
  } catch { /* 拦截器已提示 */ }
}

async function disable(e: QaCacheEntry): Promise<void> {
  try {
    await ElMessageBox.confirm('下线后该问答将不再命中，确定下线吗？', '下线确认', {
      type: 'warning', confirmButtonText: '下线', cancelButtonText: '取消',
    })
  } catch { return }
  try {
    await disableEntry(e.id, 'MANUAL')
    ElMessage.success('已下线')
    void load()
  } catch { /* 拦截器已提示 */ }
}

function isPending(s: CacheStatus): boolean { return s === 'PENDING' }
function isOnline(s: CacheStatus): boolean { return s === 'ACTIVE' || s === 'APPROVED' }

onMounted(load)
</script>

<template>
  <div class="ke-page qc">
    <header class="qc__head">
      <div>
        <h1 class="qc__title">问答审核</h1>
        <p class="qc__sub ke-muted">高频问答的挖掘、审核、上线与准出全生命周期</p>
      </div>
      <el-button :icon="'Refresh'" @click="load">刷新</el-button>
    </header>

    <section class="qc__toolbar ke-card" aria-label="筛选">
      <el-select v-model="query.status" placeholder="全部状态" clearable class="qc__status" @change="search">
        <el-option v-for="o in statusOptions" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <span class="qc__count ke-muted">共 {{ total }} 条</span>
    </section>

    <section class="qc__table ke-card" aria-label="审核列表">
      <el-table v-loading="loading" :data="rows" row-key="id">
        <template #empty><el-empty description="暂无待审核内容" /></template>

        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="qc__detail">
              <p class="qc__detail-label">标准答案</p>
              <div class="qc__answer">{{ row.answer }}</div>
              <dl class="qc__meta">
                <div><dt>审核人</dt><dd>{{ row.reviewer || '—' }}</dd></div>
                <div><dt>审核备注</dt><dd>{{ row.review_note || '—' }}</dd></div>
                <div v-if="row.disable_reason"><dt>下线原因</dt><dd>{{ row.disable_reason }}</dd></div>
                <div><dt>创建时间</dt><dd>{{ formatDateTime(row.created_at) }}</dd></div>
                <div><dt>上线时间</dt><dd>{{ formatDateTime(row.activated_at) }}</dd></div>
              </dl>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="问题" min-width="280">
          <template #default="{ row }"><span class="qc__q">{{ row.question }}</span></template>
        </el-table-column>

        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="CACHE_STATUS_TYPE[row.status as CacheStatus]" effect="light">
              {{ CACHE_STATUS_LABEL[row.status as CacheStatus] ?? row.status }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="频次" width="80" align="right" prop="frequency" />
        <el-table-column label="命中" width="80" align="right" prop="hits" />
        <el-table-column label="质量分" width="90" align="right">
          <template #default="{ row }">{{ row.quality?.toFixed?.(2) ?? row.quality }}</template>
        </el-table-column>

        <el-table-column label="操作" width="190" fixed="right">
          <template #default="{ row }">
            <template v-if="isPending(row.status)">
              <el-button link type="primary" size="small" @click="approve(row as QaCacheEntry)">通过</el-button>
              <el-button link type="danger" size="small" @click="reject(row as QaCacheEntry)">驳回</el-button>
            </template>
            <el-button v-else-if="isOnline(row.status)" link size="small" @click="disable(row as QaCacheEntry)">下线</el-button>
            <span v-else class="ke-muted qc__none">—</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="qc__pager">
        <el-pagination
          v-model:current-page="query.page"
          v-model:page-size="query.size"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="load"
          @size-change="search"
        />
      </div>
    </section>
  </div>
</template>

<style scoped lang="scss">
.qc {
  &__head { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: var(--ke-space-5); }
  &__title { margin: 0; font-size: 20px; font-weight: 600; letter-spacing: -0.01em; color: var(--ke-gray-900); }
  &__sub { margin: 4px 0 0; font-size: 13px; }

  &__toolbar { display: flex; align-items: center; justify-content: space-between; padding: var(--ke-space-4); margin-bottom: var(--ke-space-4); }
  &__status { width: 160px; }
  &__count { font-size: 13px; }

  &__table { padding: var(--ke-space-2) var(--ke-space-2) var(--ke-space-4); }
  &__q { color: var(--ke-gray-800); }

  &__detail { padding: var(--ke-space-3) var(--ke-space-6); }
  &__detail-label { margin: 0 0 6px; font-size: 12px; color: var(--ke-gray-500); }
  &__answer {
    white-space: pre-wrap; background: var(--ke-gray-50); border: 1px solid var(--ke-gray-200);
    border-radius: var(--ke-radius-sm); padding: var(--ke-space-3); font-size: 13px; color: var(--ke-gray-700);
  }
  &__meta { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--ke-space-2) var(--ke-space-6); margin: var(--ke-space-4) 0 0; }
  &__meta dt { font-size: 12px; color: var(--ke-gray-400); }
  &__meta dd { margin: 2px 0 0; font-size: 13px; color: var(--ke-gray-700); }

  &__none { color: var(--ke-gray-300); }
  &__pager { display: flex; justify-content: flex-end; margin-top: var(--ke-space-4); padding: 0 var(--ke-space-2); }
}
</style>
