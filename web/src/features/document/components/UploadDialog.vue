<script setup lang="ts">
// 上传对话框：选择文件 + 切片/入库配置，驱动分片上传编排
import { reactive, ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { UploadFile, UploadUserFile } from 'element-plus'
import { useChunkedUpload } from '../composables/useChunkedUpload'
import { formatBytes } from '@/utils/format'
import type { KnowledgeBaseType, SplitType } from '@/types/common'
import type { UploadResult } from '../composables/useChunkedUpload'

const props = defineProps<{
  /** 传入则为「为既有文档上传新版本」，否则新建 */
  docId?: number | null
}>()

const emit = defineEmits<{
  (e: 'success', result: UploadResult): void
}>()

// 对话框显隐由父组件 v-model 控制
const visible = defineModel<boolean>({ default: false })

const { progress, busy, start, cancel } = useChunkedUpload()
const fileList = ref<UploadUserFile[]>([])
const rawFile = ref<File | null>(null)

const form = reactive({
  knowledge_base_type: 'DOCUMENT_SEARCH' as KnowledgeBaseType,
  split_type: 'SMART' as SplitType,
  chunk_size: 800,
  overlap: 0,
  title_level: 0,
  regex: '',
  separator: '',
  description: '',
  accessible_by: '',
  expire_date: '',
  changelog: '',
})

const isTitle = computed(() => form.split_type === 'TITLE')
const isRegex = computed(() => form.split_type === 'REGEX')
const isSep = computed(() => form.split_type === 'SEPARATOR')

const phaseText = computed(() => {
  switch (progress.value.phase) {
    case 'hashing': return `正在校验文件 ${progress.value.hashPercent}%`
    case 'uploading': return `上传分片 ${progress.value.uploaded}/${progress.value.total}`
    case 'finalizing': return '合并入库并触发切片向量化…'
    default: return ''
  }
})

function onFileChange(file: UploadFile): void {
  rawFile.value = file.raw ?? null
  fileList.value = file.raw ? [file] : []
}

function onFileRemove(): void {
  rawFile.value = null
  fileList.value = []
}

async function submit(): Promise<void> {
  if (!rawFile.value) {
    ElMessage.warning('请先选择要上传的文件')
    return
  }
  try {
    const result = await start(rawFile.value, {
      split_type: form.split_type,
      chunk_size: form.chunk_size,
      overlap: form.overlap,
      title_level: form.title_level,
      regex: isRegex.value ? form.regex : null,
      separator: isSep.value ? form.separator : null,
      knowledge_base_type: form.knowledge_base_type,
      accessible_by: form.accessible_by || null,
      expire_date: form.expire_date || null,
      description: form.description || null,
    }, props.docId ?? null)

    if (result.instantHit) ElMessage.success('检测到相同文件，已复用已有文档（未重复入库）')
    else ElMessage.success('上传成功，正在后台解析入库')
    emit('success', result)
    visible.value = false
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') {
      ElMessage.info('已取消上传')
    }
    // 其余错误已由 http 拦截器提示
  }
}

function resetPicked(): void {
  // 关闭/重开对话框时清空已选文件，避免下次打开仍显示上一次的文件（像已上传过一样）
  rawFile.value = null
  fileList.value = []
}

// 每次打开对话框都从干净状态开始：上一次上传的文件不应再出现在列表里
watch(visible, (v) => {
  if (v) resetPicked()
})

function close(): void {
  if (busy.value) cancel()
  visible.value = false
  resetPicked()
}
</script>

<template>
  <el-dialog v-model="visible" :title="docId ? '上传新版本' : '上传知识文档'"
             width="640px" :close-on-click-modal="false" :before-close="close" append-to-body>
    <el-form label-position="top" :disabled="busy">
      <el-form-item label="文件" required>
        <el-upload
          v-model:file-list="fileList"
          :auto-upload="false"
          :limit="1"
          drag
          :on-change="onFileChange"
          :on-remove="onFileRemove"
          class="up__drop">
          <el-icon class="up__drop-icon"><UploadFilled /></el-icon>
          <div class="up__drop-text">
            拖拽文件到此处，或 <em>点击选择</em>
            <p class="up__drop-hint ke-muted">支持 PDF / Markdown / Excel / 文本，大文件自动分片上传</p>
          </div>
        </el-upload>
        <p v-if="rawFile" class="up__picked">已选：{{ rawFile.name }}（{{ formatBytes(rawFile.size) }}）</p>
      </el-form-item>

      <div class="up__grid">
        <el-form-item label="知识库类型">
          <el-select v-model="form.knowledge_base_type">
            <el-option label="文档检索" value="DOCUMENT_SEARCH" />
            <el-option label="数据查询" value="DATA_QUERY" />
          </el-select>
        </el-form-item>
        <el-form-item label="切片策略">
          <el-select v-model="form.split_type">
            <el-option label="智能切片" value="SMART" />
            <el-option label="按长度" value="LENGTH" />
            <el-option label="按标题（Markdown）" value="TITLE" />
            <el-option label="按正则" value="REGEX" />
            <el-option label="按分隔符" value="SEPARATOR" />
          </el-select>
        </el-form-item>
      </div>

      <div class="up__grid">
        <el-form-item label="分片长度">
          <el-input-number v-model="form.chunk_size" :min="50" :max="4000" :step="100" controls-position="right" />
        </el-form-item>
        <el-form-item label="重叠长度">
          <el-input-number v-model="form.overlap" :min="0" :max="1000" :step="50" controls-position="right" />
        </el-form-item>
        <el-form-item v-if="isTitle" label="标题层级">
          <el-input-number v-model="form.title_level" :min="0" :max="6" controls-position="right" />
        </el-form-item>
      </div>

      <el-form-item v-if="isRegex" label="正则表达式">
        <el-input v-model="form.regex" placeholder="用于切分的正则，例如分段标记" />
      </el-form-item>
      <el-form-item v-if="isSep" label="分隔符">
        <el-input v-model="form.separator" placeholder="用于切分的分隔字符串" />
      </el-form-item>

      <div class="up__grid">
        <el-form-item label="可访问角色">
          <el-input v-model="form.accessible_by" placeholder="留空表示公开，如：vip,normal" />
        </el-form-item>
        <el-form-item label="有效期">
          <el-date-picker v-model="form.expire_date" type="datetime" value-format="YYYY-MM-DDTHH:mm:ss"
                          placeholder="不过期可留空" class="up__date" />
        </el-form-item>
      </div>

      <el-form-item label="描述">
        <el-input v-model="form.description" type="textarea" :rows="2" maxlength="512" show-word-limit
                  placeholder="文档内容简介（可选）" />
      </el-form-item>

      <!-- 进度反馈 -->
      <div v-if="busy" class="up__progress">
        <p class="up__progress-text">{{ phaseText }}</p>
        <el-progress :percentage="progress.uploadPercent" :stroke-width="8" />
      </div>
    </el-form>

    <template #footer>
      <el-button v-if="busy" @click="cancel">取消</el-button>
      <el-button v-else @click="visible = false">关闭</el-button>
      <el-button type="primary" :loading="busy" :disabled="!rawFile" @click="submit">
        {{ docId ? '上传新版本' : '开始上传' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped lang="scss">
.up {
  &__drop { width: 100%; :deep(.el-upload-dragger) { padding: var(--ke-space-5); } }
  &__drop-icon { font-size: 40px; color: var(--ke-gray-300); margin-bottom: 6px; }
  &__drop-text { font-size: 14px; color: var(--ke-gray-600); em { color: var(--ke-brand-500); font-style: normal; } }
  &__drop-hint { margin: 6px 0 0; font-size: 12px; }
  &__picked { margin: var(--ke-space-2) 0 0; font-size: 13px; color: var(--ke-gray-600); }

  &__grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0 var(--ke-space-4); }
  &__date { width: 100%; }

  &__progress { margin-top: var(--ke-space-2); }
  &__progress-text { margin: 0 0 var(--ke-space-2); font-size: 13px; color: var(--ke-brand-600); }
}

/* ---------- H5：表单单列排布，拖拽区收紧 ---------- */
@media (max-width: 768px) {
  .up__grid { grid-template-columns: 1fr; }
  .up__grid :deep(.el-input-number) { width: 100%; }
  .up__drop :deep(.el-upload-dragger) { padding: 20px 12px; }
  .up__drop-icon { font-size: 32px; }
  .up__drop-hint { font-size: 11px; }
}
</style>
