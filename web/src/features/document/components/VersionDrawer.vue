<script setup lang="ts">
// 版本治理抽屉：列出历史版本 + 处理流程时间线，支持预览/激活/删除/重试/重新切片
import { computed, ref, reactive, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getDocument, listVersions, switchVersion, deleteVersion, retryPipeline, resplit } from '@/api/document'
import { DOC_STATUS_LABEL, DOC_STATUS_TYPE, formatDateTime } from '@/utils/format'
import type {
  DocumentVO, DocumentVersionVO, SplitConfig, VersionExtension,
  PipelineStage, PipelineStageStatus,
} from '@/types/document'
import type { SplitType } from '@/types/common'
import PreviewDialog from './PreviewDialog.vue'
import ChunkInspector from './ChunkInspector.vue'

const props = defineProps<{ doc: DocumentVO | null }>()
const emit = defineEmits<{ (e: 'updated'): void; (e: 'upload-version'): void }>()
const visible = defineModel<boolean>({ default: false })

const versions = ref<DocumentVersionVO[]>([])
/** 打开抽屉时重新拉取的文档详情（父组件传入的 doc 可能已过期） */
const detail = ref<DocumentVO | null>(null)
const loading = ref(false)

const currentId = computed(() => detail.value?.current_version_id ?? props.doc?.current_version_id ?? null)
const lockVersion = computed(() => detail.value?.lock_version ?? props.doc?.lock_version ?? 0)
const fileType = computed(() => detail.value?.extension?.fileType ?? null)

const STAGE_LABEL: Record<PipelineStage, string> = { convert: '转换', split: '切片', embedding: '向量化' }
const STAGE_STATUS_LABEL: Record<PipelineStageStatus, string> = {
  STARTED: '进行中', SUCCEEDED: '已完成', FAILED: '失败',
}
const STAGE_TAG_TYPE: Record<PipelineStageStatus, 'primary' | 'success' | 'danger'> = {
  STARTED: 'primary', SUCCEEDED: 'success', FAILED: 'danger',
}

async function load(): Promise<void> {
  const docId = props.doc?.doc_id
  if (!docId) return
  loading.value = true
  try {
    const [list, doc] = await Promise.all([listVersions(docId), getDocument(docId)])
    versions.value = list
    detail.value = doc
  } catch {
    /* 拦截器已提示 */
  } finally {
    loading.value = false
  }
}

watch(() => [visible.value, props.doc?.doc_id], () => { if (visible.value) void load() })

/** 取某版本的阶段轨迹（来自 document.extension.versions） */
function extOf(v: DocumentVersionVO): VersionExtension | null {
  return detail.value?.extension?.versions?.[String(v.version_id)] ?? null
}

async function activate(v: DocumentVersionVO): Promise<void> {
  if (!props.doc) return
  try {
    await switchVersion(props.doc.doc_id, v.version_id, lockVersion.value)
    ElMessage.success('已切换激活版本')
    emit('updated')
    await load()
  } catch { /* 拦截器已提示 */ }
}

async function remove(v: DocumentVersionVO): Promise<void> {
  if (!props.doc) return
  try {
    await ElMessageBox.confirm(`确定删除版本 ${v.version} 吗？该操作会回滚此版本。`, '删除版本', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
  } catch { return }
  try {
    await deleteVersion(props.doc.doc_id, v.version_id)
    ElMessage.success('版本已删除')
    emit('updated')
    await load()
  } catch { /* 拦截器已提示 */ }
}

async function retry(v: DocumentVersionVO, stage: string): Promise<void> {
  if (!props.doc) return
  try {
    await retryPipeline(props.doc.doc_id, v.version_id, stage)
    ElMessage.success(`已重新触发${STAGE_LABEL[stage as PipelineStage] ?? stage}`)
    emit('updated')
    await load()
  } catch { /* 拦截器已提示 */ }
}

/** 重试失败阶段：优先用阶段轨迹里记录的 failedStage，缺省从转换重跑 */
function retryFailed(v: DocumentVersionVO): void {
  const stage = extOf(v)?.failedStage ?? 'convert'
  void retry(v, stage)
}

// 预览
const previewOpen = ref(false)
const previewTarget = ref<DocumentVersionVO | null>(null)

function openPreview(v: DocumentVersionVO): void {
  previewTarget.value = v
  previewOpen.value = true
}

// 切片 / 向量化结果
const chunkOpen = ref(false)
const chunkTarget = ref<DocumentVersionVO | null>(null)

function openChunks(v: DocumentVersionVO): void {
  chunkTarget.value = v
  chunkOpen.value = true
}

// 重新切片配置弹层
const splitOpen = ref(false)
const splitTarget = ref<DocumentVersionVO | null>(null)
const splitting = ref(false)
const splitForm = reactive<SplitConfig>({
  split_type: 'SMART' as SplitType, chunk_size: 800, overlap: 0, title_level: 0, regex: null, separator: null,
})

function openResplit(v: DocumentVersionVO): void {
  splitTarget.value = v
  const conf = detail.value?.extension?.splitConfig
  Object.assign(splitForm, {
    split_type: conf?.split_type ?? 'SMART',
    chunk_size: conf?.chunk_size ?? 800,
    overlap: conf?.overlap ?? 0,
    title_level: conf?.title_level ?? 0,
    regex: conf?.regex ?? null,
    separator: conf?.separator ?? null,
  })
  splitOpen.value = true
}

async function submitResplit(): Promise<void> {
  if (!props.doc || !splitTarget.value) return
  splitting.value = true
  try {
    await resplit(props.doc.doc_id, splitTarget.value.version_id, { ...splitForm })
    ElMessage.success('已按新配置重新切片')
    splitOpen.value = false
    emit('updated')
    await load()
  } catch { /* 拦截器已提示 */ } finally {
    splitting.value = false
  }
}
</script>

<template>
  <el-drawer v-model="visible" :title="doc ? `版本管理 · ${doc.doc_title}` : '版本管理'" size="620px">
    <div class="vd">
      <div class="vd__bar">
        <el-button type="primary" plain size="small" @click="emit('upload-version')">
          <el-icon><Upload /></el-icon>&nbsp;上传新版本
        </el-button>
        <el-button size="small" :icon="'Refresh'" @click="load">刷新</el-button>
      </div>

      <el-skeleton v-if="loading && !versions.length" :rows="4" animated />

      <el-empty v-else-if="!versions.length" description="暂无版本记录" />

      <ul v-else class="vd__list">
        <li v-for="v in versions" :key="v.version_id" class="vd__item"
            :class="{ 'is-current': v.version_id === currentId }">
          <div class="vd__row">
            <div class="vd__meta">
              <span class="vd__ver">{{ v.version }}</span>
              <el-tag v-if="v.version_id === currentId" size="small" type="success" effect="dark">当前</el-tag>
              <el-tag size="small" :type="DOC_STATUS_TYPE[v.status]" effect="plain">{{ DOC_STATUS_LABEL[v.status] ?? v.status }}</el-tag>
            </div>
            <time class="vd__time ke-muted">{{ formatDateTime(v.created_at) }}</time>
          </div>
          <p v-if="v.changelog" class="vd__log">{{ v.changelog }}</p>

          <!-- 处理流程时间线：转换 → 切片 → 向量化，失败原因与自动补偿次数一并可见 -->
          <div class="vd__flow">
            <div class="vd__flow-head">
              <span>处理流程</span>
              <span class="ke-muted">自动补偿 {{ extOf(v)?.attempts ?? 0 }} 次</span>
            </div>
            <el-timeline v-if="extOf(v)?.stages?.length">
              <el-timeline-item v-for="(s, i) in extOf(v)!.stages" :key="i" :timestamp="s.at"
                                :type="s.status === 'FAILED' ? 'danger' : s.status === 'SUCCEEDED' ? 'success' : 'primary'">
                <span class="vd__stage">{{ STAGE_LABEL[s.stage] ?? s.stage }}</span>
                <el-tag size="small" :type="STAGE_TAG_TYPE[s.status]" effect="light">
                  {{ STAGE_STATUS_LABEL[s.status] ?? s.status }}
                </el-tag>
                <p v-if="s.detail" class="vd__detail">{{ s.detail }}</p>
              </el-timeline-item>
            </el-timeline>
            <p v-else class="vd__detail ke-muted">尚未开始处理</p>
            <el-alert v-if="extOf(v)?.lastError" :title="extOf(v)!.lastError" type="error"
                      :closable="false" show-icon class="vd__err" />
          </div>

          <div class="vd__actions">
            <el-button link type="primary" size="small" @click="openPreview(v)">预览</el-button>
            <el-button link type="primary" size="small" @click="openChunks(v)">切片详情</el-button>
            <el-button v-if="v.version_id !== currentId && v.status === 'VECTOR_STORED'"
                       link type="primary" size="small" @click="activate(v)">设为当前</el-button>
            <el-button v-if="v.status === 'FAILED'" size="small" @click="retryFailed(v)">重试失败阶段</el-button>
            <el-dropdown trigger="click">
              <el-button size="small" link><el-icon><MoreFilled /></el-icon></el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item @click="retry(v, 'convert')">重试转换</el-dropdown-item>
                  <el-dropdown-item @click="retry(v, 'split')">重试切片</el-dropdown-item>
                  <el-dropdown-item @click="retry(v, 'embedding')">重试向量化</el-dropdown-item>
                  <el-dropdown-item divided @click="openResplit(v)">重新切片…</el-dropdown-item>
                  <el-dropdown-item @click="remove(v)">删除版本</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
        </li>
      </ul>
    </div>

    <!-- 重新切片配置 -->
    <el-dialog v-model="splitOpen" title="重新切片配置" width="420px" append-to-body>
      <el-form label-position="top">
        <el-form-item label="切片策略">
          <el-select v-model="splitForm.split_type" class="vd__full">
            <el-option label="智能切片" value="SMART" />
            <el-option label="按长度" value="LENGTH" />
            <el-option label="按标题" value="TITLE" />
            <el-option label="按正则" value="REGEX" />
            <el-option label="按分隔符" value="SEPARATOR" />
          </el-select>
        </el-form-item>
        <el-form-item label="分片长度">
          <el-input-number v-model="splitForm.chunk_size" :min="50" :max="4000" :step="100" controls-position="right" />
        </el-form-item>
        <el-form-item label="重叠长度">
          <el-input-number v-model="splitForm.overlap" :min="0" :max="1000" :step="50" controls-position="right" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="splitOpen = false">取消</el-button>
        <el-button type="primary" :loading="splitting" @click="submitResplit">提交并重切片</el-button>
      </template>
    </el-dialog>

    <PreviewDialog v-model="previewOpen" :doc-id="doc?.doc_id ?? null"
                   :version-id="previewTarget?.version_id ?? null"
                   :version-label="previewTarget?.version ?? null"
                   :file-type="fileType" />
    <ChunkInspector v-model="chunkOpen" :doc-id="doc?.doc_id ?? null"
                    :version-id="chunkTarget?.version_id ?? null"
                    :version-label="chunkTarget?.version ?? null" />
  </el-drawer>
</template>

<style scoped lang="scss">
.vd {
  &__bar { display: flex; gap: var(--ke-space-2); margin-bottom: var(--ke-space-4); }
  &__list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--ke-space-3); }
  &__item {
    border: 1px solid var(--ke-gray-200); border-radius: var(--ke-radius-md);
    padding: var(--ke-space-3) var(--ke-space-4); background: #fff;
    &.is-current { border-color: var(--ke-brand-400); box-shadow: var(--ke-shadow-sm); }
  }
  &__row { display: flex; align-items: center; justify-content: space-between; }
  &__meta { display: flex; align-items: center; gap: var(--ke-space-2); }
  &__ver { font-weight: 600; color: var(--ke-gray-800); font-size: 14px; }
  &__time { font-size: 12px; }
  &__log { margin: var(--ke-space-2) 0 0; font-size: 13px; color: var(--ke-gray-500); }
  &__flow {
    margin-top: var(--ke-space-2);
    padding: var(--ke-space-3);
    background: var(--ke-gray-50, #f8fafc);
    border-radius: var(--ke-radius-sm);
  }
  &__flow-head { display: flex; justify-content: space-between; font-size: 12px; color: var(--ke-gray-600); }
  &__stage { margin-right: var(--ke-space-2); font-size: 13px; color: var(--ke-gray-700); }
  &__detail { margin: 4px 0 0; font-size: 12px; color: var(--ke-gray-500); word-break: break-all; }
  &__err { margin-top: var(--ke-space-2); }
  &__actions { display: flex; align-items: center; gap: var(--ke-space-2); margin-top: var(--ke-space-3); }
  &__full { width: 100%; }
}
</style>
