<script setup lang="ts">
// 日志中心：操作日志（MySQL） + 系统日志（Elasticsearch 集中检索）
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { listLogModules, listOperationLogs, listSystemLogs } from '@/api/admin'
import type { OperationLogVO, SystemLogVO } from '@/types/admin'
import { formatDateTime } from '@/utils/format'
import { useBreakpoint } from '@/composables/useBreakpoint'

const { isMobile } = useBreakpoint()
const tab = ref<'operations' | 'system'>('operations')

const modules = ref<string[]>([])
const operations = ref<OperationLogVO[]>([])
const systemLogs = ref<SystemLogVO[]>([])
const total = ref(0)
const loading = ref(false)
const degraded = ref(false)
const degradeReason = ref('')

const query = reactive({
  page: 1,
  size: 10,
  keyword: '',
  module: '',
  status: '',
  level: '',
  request_id: '',
  range: [] as string[],
})

const detailOpen = ref(false)
const detailTitle = ref('')
const detailBody = ref('')

const LEVEL_TYPE: Record<string, 'info' | 'warning' | 'danger' | 'success'> = {
  DEBUG: 'info', INFO: 'success', WARNING: 'warning', ERROR: 'danger', CRITICAL: 'danger',
}

const moduleOptions = computed(() => modules.value)

/** 日期范围 → 后端可解析的 start / end */
function timeParams(): { start?: string; end?: string } {
  const [start, end] = query.range ?? []
  return { start: start || undefined, end: end || undefined }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const base = { page: query.page, size: query.size, keyword: query.keyword.trim() || undefined,
      ...timeParams() }
    if (tab.value === 'operations') {
      const res = await listOperationLogs({
        ...base,
        module: query.module || undefined,
        status: query.status || undefined,
      })
      operations.value = res.records
      total.value = res.total
      degraded.value = false
    } else {
      const res = await listSystemLogs({
        ...base,
        level: query.level || undefined,
        request_id: query.request_id.trim() || undefined,
        module: query.module || undefined,
        ...timeParams(),
      })
      systemLogs.value = res.records
      total.value = res.total
      degraded.value = res.degraded
      degradeReason.value = res.reason ?? ''
    }
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

function reset(): void {
  query.keyword = ''
  query.module = ''
  query.status = ''
  query.level = ''
  query.request_id = ''
  query.range = []
  search()
}

function onPageChange(page: number): void {
  query.page = page
  void load()
}

function openDetail(title: string, payload: unknown): void {
  detailTitle.value = title
  try {
    detailBody.value = JSON.stringify(payload, null, 2)
  } catch {
    detailBody.value = String(payload)
  }
  detailOpen.value = true
}

/** 模块/动作展示：`auth:login` 这类编码保持不变，中文语义由后端 detail 提供 */
function actionLabel(row: OperationLogVO): string {
  return `${row.module} · ${row.action}`
}

function accountLabel(row: OperationLogVO): string {
  return row.account_name || (row.account_id != null ? `#${row.account_id}` : '匿名')
}

function levelLabel(row: SystemLogVO): string {
  return String(row.level ?? 'INFO').toUpperCase()
}

function eventText(row: SystemLogVO): string {
  return String(row.event ?? '')
}

watch(tab, () => {
  operations.value = []
  systemLogs.value = []
  total.value = 0
  query.page = 1
  void load()
})

onMounted(async () => {
  void load()
  try {
    modules.value = await listLogModules()
  } catch {
    /* 下拉失败不影响主流程 */
  }
})
</script>

<template>
  <div class="ke-page logs">
    <header class="logs__head">
      <div>
        <h1 class="logs__title">日志中心</h1>
        <p class="logs__sub ke-muted">
          操作日志回答「谁做了什么」，系统日志集中在 Elasticsearch，可跨实例检索同一 request_id
        </p>
      </div>
      <el-segmented
        v-model="tab"
        :options="[
          { label: '操作日志', value: 'operations' },
          { label: '系统日志', value: 'system' },
        ]"
        aria-label="日志类型"
      />
    </header>

    <section class="logs__toolbar ke-card" aria-label="筛选">
      <el-input
        v-model="query.keyword"
        class="logs__kw"
        :placeholder="tab === 'operations' ? '账号 / 动作 / 路径 / request_id' : '事件 / 路径 / logger'"
        clearable
        @keyup.enter="search"
        @clear="search"
        aria-label="搜索关键字"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>

      <el-select v-model="query.module" clearable placeholder="模块" class="logs__select"
                 aria-label="按模块筛选" @change="search">
        <el-option v-for="item in moduleOptions" :key="item" :label="item" :value="item" />
      </el-select>

      <el-select v-if="tab === 'operations'" v-model="query.status" clearable
                 placeholder="结果" class="logs__select" aria-label="按结果筛选" @change="search">
        <el-option label="成功" value="SUCCESS" />
        <el-option label="失败" value="FAILED" />
      </el-select>

      <el-select v-else v-model="query.level" clearable placeholder="级别" class="logs__select"
                 aria-label="按级别筛选" @change="search">
        <el-option v-for="lv in ['DEBUG', 'INFO', 'WARNING', 'ERROR']" :key="lv" :label="lv" :value="lv" />
      </el-select>

      <el-input v-if="tab === 'system'" v-model="query.request_id" class="logs__select"
                placeholder="request_id" clearable @keyup.enter="search" @clear="search" />

      <el-date-picker
        v-model="query.range"
        type="datetimerange"
        value-format="YYYY-MM-DD HH:mm:ss"
        range-separator="~"
        start-placeholder="开始时间"
        end-placeholder="结束时间"
        class="logs__range"
        @change="search"
      />

      <div class="logs__actions">
        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="reset">重置</el-button>
        <el-button @click="load">刷新</el-button>
      </div>
    </section>

    <el-alert
      v-if="tab === 'system' && degraded"
      class="logs__alert"
      type="warning"
      :closable="false"
      show-icon
      :title="degradeReason || 'Elasticsearch 不可用，暂时只能查看本地日志文件'"
    />

    <!-- ---------------- 操作日志 ---------------- -->
    <section v-if="tab === 'operations'" class="ke-card ke-card--table" aria-label="操作日志列表">
      <div v-if="isMobile" v-loading="loading" class="logs__cards">
        <el-empty v-if="!operations.length" description="暂无操作日志" />
        <ul v-else class="ke-mlist">
          <li v-for="row in operations" :key="row.id" class="ke-mcard">
            <div class="ke-mcard__head">
              <span class="ke-mcard__title">{{ actionLabel(row) }}</span>
              <el-tag size="small" :type="row.status === 'SUCCESS' ? 'success' : 'danger'" effect="light">
                {{ row.status === 'SUCCESS' ? '成功' : '失败' }}
              </el-tag>
            </div>
            <div class="ke-mfacts">
              <div class="ke-mfact">
                <span class="ke-mfact__label">操作人</span>
                <span class="ke-mfact__value">{{ accountLabel(row) }}</span>
              </div>
              <div class="ke-mfact">
                <span class="ke-mfact__label">时间</span>
                <span class="ke-mfact__value">{{ formatDateTime(row.created_at) }}</span>
              </div>
              <div class="ke-mfact">
                <span class="ke-mfact__label">请求</span>
                <span class="ke-mfact__value">{{ row.method }} {{ row.path }}</span>
              </div>
              <div class="ke-mfact">
                <span class="ke-mfact__label">request_id</span>
                <span class="ke-mfact__value">{{ row.request_id || '—' }}</span>
              </div>
            </div>
            <div class="ke-mcard__actions">
              <el-button link type="primary" size="small"
                         @click="openDetail('操作详情', row)">详情</el-button>
            </div>
          </li>
        </ul>
      </div>

      <el-table v-else v-loading="loading" :data="operations" stripe row-key="id">
        <template #empty><el-empty description="暂无操作日志" /></template>
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作人" min-width="120">
          <template #default="{ row }">
            <div class="logs__who">
              <span>{{ accountLabel(row as OperationLogVO) }}</span>
              <span class="logs__who-id">{{ row.identity || '—' }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="模块 / 动作" min-width="160">
          <template #default="{ row }">{{ actionLabel(row as OperationLogVO) }}</template>
        </el-table-column>
        <el-table-column label="请求" min-width="200">
          <template #default="{ row }">
            <span class="logs__mono">{{ row.method }} {{ row.path }}</span>
          </template>
        </el-table-column>
        <el-table-column label="结果" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'SUCCESS' ? 'success' : 'danger'" effect="light">
              {{ row.status === 'SUCCESS' ? '成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="90" align="right">
          <template #default="{ row }">{{ row.duration_ms != null ? `${row.duration_ms} ms` : '—' }}</template>
        </el-table-column>
        <el-table-column label="request_id" min-width="170">
          <template #default="{ row }">
            <span class="logs__mono">{{ row.request_id || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="详情" width="80" align="center">
          <template #default="{ row }">
            <el-button link type="primary" size="small"
                       @click="openDetail('操作详情', row)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <!-- ---------------- 系统日志 ---------------- -->
    <section v-else class="ke-card ke-card--table" aria-label="系统日志列表">
      <div v-if="isMobile" v-loading="loading" class="logs__cards">
        <el-empty v-if="!systemLogs.length" description="暂无系统日志" />
        <ul v-else class="ke-mlist">
          <li v-for="(row, idx) in systemLogs" :key="row._id || idx" class="ke-mcard">
            <div class="ke-mcard__head">
              <span class="ke-mcard__title">{{ eventText(row) }}</span>
              <el-tag size="small" :type="LEVEL_TYPE[levelLabel(row)] || 'info'" effect="light">
                {{ levelLabel(row) }}
              </el-tag>
            </div>
            <div class="ke-mfacts">
              <div class="ke-mfact">
                <span class="ke-mfact__label">时间</span>
                <span class="ke-mfact__value">{{ formatDateTime(row['@timestamp']) }}</span>
              </div>
              <div class="ke-mfact">
                <span class="ke-mfact__label">实例</span>
                <span class="ke-mfact__value">{{ row.instance || row.host || '—' }}</span>
              </div>
              <div class="ke-mfact">
                <span class="ke-mfact__label">request_id</span>
                <span class="ke-mfact__value">{{ row.request_id || '—' }}</span>
              </div>
            </div>
            <div class="ke-mcard__actions">
              <el-button link type="primary" size="small"
                         @click="openDetail('系统日志原文', row)">详情</el-button>
            </div>
          </li>
        </ul>
      </div>

      <el-table v-else v-loading="loading" :data="systemLogs" stripe row-key="_id">
        <template #empty><el-empty description="暂无系统日志" /></template>
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ formatDateTime(row['@timestamp']) }}</template>
        </el-table-column>
        <el-table-column label="级别" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="LEVEL_TYPE[levelLabel(row as SystemLogVO)] || 'info'" effect="light">
              {{ levelLabel(row as SystemLogVO) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="事件" min-width="200">
          <template #default="{ row }">
            <span class="logs__mono">{{ eventText(row as SystemLogVO) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="logger" min-width="150">
          <template #default="{ row }">{{ row.logger || '—' }}</template>
        </el-table-column>
        <el-table-column label="请求" min-width="200">
          <template #default="{ row }">
            <span class="logs__mono">{{ row.method || '' }} {{ row.path || '' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="90" align="right">
          <template #default="{ row }">{{ row.duration_ms != null ? `${row.duration_ms} ms` : '—' }}</template>
        </el-table-column>
        <el-table-column label="request_id" min-width="170">
          <template #default="{ row }">
            <span class="logs__mono">{{ row.request_id || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="实例" min-width="150">
          <template #default="{ row }">{{ row.instance || row.host || '—' }}</template>
        </el-table-column>
        <el-table-column label="详情" width="80" align="center">
          <template #default="{ row }">
            <el-button link type="primary" size="small"
                       @click="openDetail('系统日志原文', row)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <footer class="logs__foot">
      <el-pagination
        :current-page="query.page"
        :page-size="query.size"
        :total="total"
        background
        layout="total, prev, pager, next"
        @current-change="onPageChange"
      />
    </footer>

    <el-dialog v-model="detailOpen" :title="detailTitle" width="min(720px, 94vw)" append-to-body>
      <pre class="logs__detail">{{ detailBody }}</pre>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.logs {
  &__head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--ke-space-4);
    flex-wrap: wrap;
    margin-bottom: var(--ke-space-4);
  }
  &__title {
    margin: 0 0 4px;
    font-size: 22px;
    color: var(--ke-gray-900);
  }
  &__sub {
    margin: 0;
    font-size: 13px;
  }

  &__toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ke-space-3);
    margin-bottom: var(--ke-space-4);
  }
  &__kw { width: 260px; }
  &__select { width: 130px; }
  &__range { width: 330px; }
  &__actions { display: flex; gap: var(--ke-space-2); margin-left: auto; }

  &__alert { margin-bottom: var(--ke-space-3); }

  &__who {
    display: flex;
    flex-direction: column;
    line-height: 1.3;
  }
  &__who-id { font-size: 11px; color: var(--ke-gray-400); }

  &__mono {
    font-family: var(--ke-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
    font-size: 12px;
    word-break: break-all;
    color: var(--ke-gray-700);
  }

  &__foot {
    display: flex;
    justify-content: flex-end;
    padding: var(--ke-space-3) 0;
  }

  &__detail {
    max-height: 60vh;
    overflow: auto;
    margin: 0;
    padding: var(--ke-space-3);
    border-radius: var(--ke-radius-sm);
    background: var(--ke-gray-50);
    font-size: 12px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-all;
  }

  @media (max-width: 768px) {
    &__kw, &__select, &__range { width: 100%; }
    &__actions { margin-left: 0; width: 100%; }
  }
}
</style>
