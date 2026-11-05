<script setup lang="ts">
// 知识文档列表：筛选 / 分页 / 上传 / 版本治理 / 删除；处理中自动轮询刷新
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listDocuments, deleteDocument, retryPipeline } from '@/api/document'
import type { DocumentVO } from '@/types/document'
import type { DocumentStatus } from '@/types/common'
import { DOC_STATUS_LABEL, DOC_STATUS_TYPE, DOC_PROCESSING, formatDateTime } from '@/utils/format'
import UploadDialog from './components/UploadDialog.vue'
import VersionDrawer from './components/VersionDrawer.vue'
import PreviewDialog from './components/PreviewDialog.vue'
import type { UploadResult } from './composables/useChunkedUpload'
import { useBreakpoint } from '@/composables/useBreakpoint'

const { isMobile } = useBreakpoint()
const rows = ref<DocumentVO[]>([])
const total = ref(0)
const loading = ref(false)
const query = reactive({ page: 1, size: 10, status: '' as '' | DocumentStatus, keyword: '' })

const uploadOpen = ref(false)
const uploadDocId = ref<number | null>(null)
const drawerOpen = ref(false)
const drawerDoc = ref<DocumentVO | null>(null)
const previewOpen = ref(false)
const previewRow = ref<DocumentVO | null>(null)

let pollTimer: ReturnType<typeof setInterval> | null = null

const statusOptions = computed(() =>
  Object.entries(DOC_STATUS_LABEL).map(([value, label]) => ({ value, label })))

async function load(silent = false): Promise<void> {
  if (!silent) loading.value = true
  try {
    const res = await listDocuments({
      page: query.page, size: query.size,
      status: query.status || undefined, keyword: query.keyword.trim() || undefined,
    })
    rows.value = res.records
    total.value = res.total
  } catch {
    /* 拦截器已提示 */
  } finally {
    if (!silent) loading.value = false
    syncPolling()
  }
}

function search(): void {
  query.page = 1
  void load()
}

function reset(): void {
  query.status = ''
  query.keyword = ''
  query.page = 1
  void load()
}

/** 分页页码变化：v-model 已同步 query.page，这里仅触发重载（忽略事件传入的页码参数） */
function reload(): void { void load() }

/** 存在处理中的文档时开启轻量轮询，全部就绪后自动停止 */
function syncPolling(): void {
  const hasProcessing = rows.value.some((r) => DOC_PROCESSING.includes(r.status))
  if (hasProcessing && !pollTimer) {
    pollTimer = setInterval(() => void load(true), 4000)
  } else if (!hasProcessing && pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function openUpload(docId: number | null = null): void {
  uploadDocId.value = docId
  uploadOpen.value = true
}

function onUploaded(result: UploadResult): void {
  if (drawerOpen.value && result.docId && drawerDoc.value?.doc_id === result.docId) {
    drawerOpen.value = false
  }
  query.page = 1
  void load()
}

async function removeDoc(doc: DocumentVO): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除文档「${doc.doc_title}」吗？将同步清理其全部版本、分段、存储文件与向量索引。`, '删除文档', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
  } catch { return }
  try {
    await deleteDocument(doc.doc_id, doc.lock_version)
    ElMessage.success('文档已删除')
    if (rows.value.length === 1 && query.page > 1) query.page -= 1
    void load()
  } catch { /* 拦截器已提示 */ }
}

async function quickRetry(doc: DocumentVO): Promise<void> {
  if (!doc.current_version_id) {
    ElMessage.warning('该文档尚无可用版本，请重新上传')
    return
  }
  try {
    await retryPipeline(doc.doc_id, doc.current_version_id, 'convert')
    ElMessage.success('已重新触发处理')
    void load(true)
  } catch { /* 拦截器已提示 */ }
}

function openVersions(doc: DocumentVO): void {
  drawerDoc.value = doc
  drawerOpen.value = true
}

/** 当前版本的失败原因（来自 extension.versions 阶段轨迹） */
function errorOf(row: DocumentVO): string | null {
  const vid = row.current_version_id
  if (!vid) return null
  return row.extension?.versions?.[String(vid)]?.lastError ?? null
}

function openPreview(row: DocumentVO): void {
  if (!row.current_version_id) {
    ElMessage.warning('该文档暂无可用版本，请重新上传')
    return
  }
  previewRow.value = row
  previewOpen.value = true
}

onMounted(async () => {
  await load()
  syncPolling()
})

onBeforeUnmount(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<template>
  <div class="ke-page doc">
    <header class="doc__head">
      <div>
        <h1 class="doc__title">知识文档</h1>
        <p class="doc__sub ke-muted">接入、切片、向量化与多版本治理</p>
      </div>
      <el-button type="primary" :icon="'Upload'" @click="openUpload(null)">上传文档</el-button>
    </header>

    <section class="doc__flow ke-card" aria-label="文档处理流程">
      <span class="doc__flow-title">处理流程</span>
      <div class="doc__steps">
        <span class="doc__step"><b>1</b>转换<em>上传后自动</em></span>
        <span class="doc__arrow" aria-hidden="true">→</span>
        <span class="doc__step"><b>2</b>切片<em>自动，可手动重新切片</em></span>
        <span class="doc__arrow" aria-hidden="true">→</span>
        <span class="doc__step"><b>3</b>向量化<em>自动，失败可手动重试</em></span>
      </div>
      <span class="doc__flow-hint">手动入口：版本抽屉 → 重新切片 / 重试切片 / 重试向量化</span>
    </section>

    <section class="doc__toolbar ke-card" aria-label="筛选">
      <el-select v-model="query.status" placeholder="全部状态" clearable class="doc__status" @change="search">
        <el-option v-for="o in statusOptions" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <el-input v-model="query.keyword" placeholder="按标题搜索" clearable class="doc__kw"
                @keyup.enter="search" @clear="search" aria-label="标题关键字">
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <el-button @click="search">查询</el-button>
      <el-button text @click="reset">重置</el-button>
    </section>

    <section class="doc__table ke-card ke-card--table" aria-label="文档列表">
      <!-- H5：宽表格改为信息卡片，操作入口完整保留 -->
      <div v-if="isMobile" v-loading="loading" class="doc__cards">
        <el-empty v-if="!rows.length" description="暂无文档，点击上方上传开始接入" />
        <ul v-else class="ke-mlist">
          <li v-for="row in rows" :key="row.doc_id" class="ke-mcard">
            <div class="ke-mcard__head">
              <span class="ke-mcard__title">{{ row.doc_title }}</span>
              <el-tooltip :disabled="!errorOf(row)" :content="errorOf(row) ?? ''" placement="top">
                <el-tag size="small" :type="DOC_STATUS_TYPE[row.status as DocumentStatus]" effect="light">
                  {{ DOC_STATUS_LABEL[row.status as DocumentStatus] ?? row.status }}
                </el-tag>
              </el-tooltip>
            </div>
            <p v-if="row.description" class="ke-mcard__desc">{{ row.description }}</p>
            <div class="ke-mfacts">
              <div class="ke-mfact">
                <span class="ke-mfact__label">知识库</span>
                <span class="ke-mfact__value">
                  {{ row.knowledge_base_type === 'DATA_QUERY' ? '数据查询' : '文档检索' }}
                </span>
              </div>
              <div class="ke-mfact">
                <span class="ke-mfact__label">版本</span>
                <span class="ke-mfact__value">{{ row.current_version || '—' }}</span>
              </div>
              <div class="ke-mfact">
                <span class="ke-mfact__label">分段数</span>
                <span class="ke-mfact__value">{{ row.segment_count }}</span>
              </div>
              <div class="ke-mfact">
                <span class="ke-mfact__label">更新时间</span>
                <span class="ke-mfact__value">{{ formatDateTime(row.updated_at) }}</span>
              </div>
            </div>
            <div class="ke-mcard__actions">
              <el-button link type="primary" size="small" @click="openPreview(row)">预览</el-button>
              <el-button link type="primary" size="small" @click="openVersions(row)">版本</el-button>
              <el-button link size="small" @click="openUpload(row.doc_id)">新版本</el-button>
              <el-button v-if="row.status === 'FAILED'" link size="small" @click="quickRetry(row)">重试</el-button>
              <el-button link type="danger" size="small" @click="removeDoc(row)">删除</el-button>
            </div>
          </li>
        </ul>
      </div>

      <el-table v-else v-loading="loading" :data="rows" stripe row-key="doc_id">
        <template #empty>
          <el-empty description="暂无文档，点击右上角上传开始接入" />
        </template>

        <el-table-column label="文档" min-width="240">
          <template #default="{ row }">
            <div class="doc__cell">
              <span class="doc__name ke-truncate">{{ row.doc_title }}</span>
              <span v-if="row.description" class="doc__desc ke-truncate">{{ row.description }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="知识库" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="light"
                    :type="row.knowledge_base_type === 'DATA_QUERY' ? 'warning' : 'primary'">
              {{ row.knowledge_base_type === 'DATA_QUERY' ? '数据查询' : '文档检索' }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tooltip :disabled="!errorOf(row as DocumentVO)" :content="errorOf(row as DocumentVO) ?? ''" placement="top">
              <el-tag size="small" :type="DOC_STATUS_TYPE[row.status as DocumentStatus]" effect="light">
                {{ DOC_STATUS_LABEL[row.status as DocumentStatus] ?? row.status }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="版本" width="90">
          <template #default="{ row }">
            <span class="doc__ver">{{ row.current_version || '—' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="分段数" width="90" align="center">
          <template #default="{ row }">{{ row.segment_count }}</template>
        </el-table-column>

        <el-table-column label="更新时间" width="160">
          <template #default="{ row }">
            <span class="doc__time">{{ formatDateTime(row.updated_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <div class="ke-actions">
              <el-button link type="primary" size="small" @click="openPreview(row as DocumentVO)">预览</el-button>
              <el-button link type="primary" size="small" @click="openVersions(row as DocumentVO)">版本</el-button>
              <el-button link size="small" @click="openUpload(row.doc_id)">新版本</el-button>
              <el-button v-if="row.status === 'FAILED'" link size="small" @click="quickRetry(row as DocumentVO)">重试</el-button>
              <el-button link type="danger" size="small" @click="removeDoc(row as DocumentVO)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <div class="doc__pager">
        <el-pagination
          v-model:current-page="query.page"
          v-model:page-size="query.size"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="reload"
          @size-change="search"
        />
      </div>
    </section>

    <UploadDialog v-model="uploadOpen" :doc-id="uploadDocId" @success="onUploaded" />
    <VersionDrawer v-model="drawerOpen" :doc="drawerDoc" @updated="load(true)"
                   @upload-version="uploadDocId = drawerDoc?.doc_id ?? null; uploadOpen = true" />
    <PreviewDialog v-model="previewOpen" :doc-id="previewRow?.doc_id ?? null"
                   :version-id="previewRow?.current_version_id ?? null"
                   :version-label="previewRow?.current_version ?? null" />
  </div>
</template>

<style scoped lang="scss">
.doc {
  &__head { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: var(--ke-space-5); }
  &__title { margin: 0; font-size: 20px; font-weight: 600; letter-spacing: -0.01em; color: var(--ke-gray-900); }
  &__sub { margin: 4px 0 0; font-size: 13px; }

  &__flow {
    display: flex; align-items: center; gap: var(--ke-space-4); flex-wrap: wrap;
    padding: var(--ke-space-3) var(--ke-space-4);
    margin-bottom: var(--ke-space-4);
    background: var(--ke-gray-50);
  }
  &__flow-title { font-size: 12px; font-weight: 600; color: var(--ke-gray-500); letter-spacing: 0.02em; }
  &__steps { display: flex; align-items: center; gap: var(--ke-space-2); flex-wrap: wrap; }
  &__step {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 999px;
    background: var(--ke-bg-card); border: 1px solid var(--ke-gray-200);
    font-size: 12px; color: var(--ke-gray-700);

    b {
      width: 16px; height: 16px; border-radius: 50%;
      display: inline-grid; place-items: center;
      background: var(--ke-brand-50); color: var(--ke-brand-600);
      font-size: 11px; font-weight: 600;
    }
    em {
      font-style: normal; font-size: 11px; color: var(--ke-gray-400);
    }
  }
  &__arrow { color: var(--ke-gray-300); font-size: 12px; }
  &__flow-hint { font-size: 12px; color: var(--ke-gray-400); }

  &__toolbar {
    display: flex; align-items: center; gap: var(--ke-space-3);
    padding: var(--ke-space-4); margin-bottom: var(--ke-space-4);
  }
  &__status { width: 150px; }
  &__kw { width: 240px; }

  &__cell { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  &__name, &__desc { display: block; max-width: 100%; }
  &__name { color: var(--ke-gray-800); font-weight: 500; }
  &__desc { font-size: 12px; color: var(--ke-gray-400); }
  &__ver { color: var(--ke-gray-600); }
  &__time { display: block; white-space: nowrap; color: var(--ke-gray-500); }
  &__pager { display: flex; justify-content: flex-end; padding: var(--ke-space-4); }

  /* ---------- H5：筛选换行、流程精简、列表卡片化 ---------- */
  @media (max-width: 768px) {
    &__head { align-items: center; margin-bottom: 12px; gap: 8px; }
    &__title { font-size: 18px; }
    &__sub { font-size: 12px; }

    &__flow {
      gap: 8px;
      padding: 10px 12px;
      margin-bottom: 10px;
    }
    &__flow-title,
    &__flow-hint { width: 100%; }
    &__steps { gap: 6px; }
    // 窄屏隐藏步骤内的说明文字，只保留“序号 + 名称”
    &__step em { display: none; }

    &__toolbar {
      flex-wrap: wrap;
      gap: 8px;
      padding: 10px;
      margin-bottom: 10px;
    }
    &__status,
    &__kw { width: 100%; }
    &__toolbar .el-button { flex: 1 1 0; }
    &__toolbar .el-button + .el-button { margin-left: 0; }

    &__cards { padding: 12px; }
    &__pager { padding: 12px 0 0; justify-content: center; }
  }
}
</style>
