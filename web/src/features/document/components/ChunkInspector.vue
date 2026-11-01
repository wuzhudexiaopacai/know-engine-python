<script setup lang="ts">
// 切片与向量化结果查看器：分片正文、父子关系、向量维度/模长/前 12 维数值
import { computed, ref, watch } from 'vue'
import { listSegments } from '@/api/document'
import type { SegmentListVO, SegmentVO } from '@/types/document'

const props = defineProps<{
  docId: number | null
  versionId: number | null
  versionLabel?: string | null
}>()
const visible = defineModel<boolean>({ default: false })

const data = ref<SegmentListVO | null>(null)
const page = ref(1)
const size = 10
const loading = ref(false)
const error = ref('')

const stat = computed(() => {
  const d = data.value
  return [
    { label: '分段总数', value: d?.total ?? 0 },
    { label: '已向量化', value: d?.embedded_count ?? 0 },
    { label: '跳过向量化', value: d?.skipped_count ?? 0 },
    { label: '向量维度', value: d?.vector_dim ?? 0 },
  ]
})

async function load(): Promise<void> {
  if (!props.docId || !props.versionId) return
  loading.value = true
  error.value = ''
  try {
    data.value = await listSegments(props.docId, props.versionId, page.value, size)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载切片结果失败'
  } finally {
    loading.value = false
  }
}

watch([visible, () => props.docId, () => props.versionId], () => {
  if (visible.value) {
    page.value = 1
    void load()
  }
})
watch(page, () => { if (visible.value) void load() })

function isParent(s: SegmentVO): boolean {
  return s.skip_embedding === 1
}

function barWidth(v: number): string {
  return `${Math.min(100, Math.abs(v) * 100)}%`
}

function barClass(v: number): string {
  return v >= 0 ? 'pos' : 'neg'
}
</script>

<template>
  <el-dialog v-model="visible" :title="`切片与向量化结果${versionLabel ? ` · v${versionLabel}` : ''}`"
             width="860px" top="4vh" append-to-body destroy-on-close>
    <div class="ci">
      <div class="ci__stats">
        <div v-for="s in stat" :key="s.label" class="ci__stat">
          <span class="ci__stat-label">{{ s.label }}</span>
          <span class="ci__stat-value">{{ s.value }}</span>
        </div>
      </div>

      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <el-alert v-else-if="data && data.vector_dim === 0 && data.total > 0" type="warning" :closable="false" show-icon
                title="当前版本还没有向量数据：流水线可能仍在向量化，或仅完成切片（可查看阶段时间线确认）" />

      <el-skeleton v-if="loading && !data" :rows="6" animated />

      <el-empty v-else-if="!loading && data && !data.records.length" description="该版本暂无分段" :image-size="80" />

      <div v-if="data && data.records.length" class="ci__list" v-loading="loading">
        <div v-for="s in data.records" :key="s.chunk_id" class="ci__item">
          <div class="ci__head">
            <span class="ci__order">#{{ s.chunk_order }}</span>
            <span class="ci__chunk">{{ s.chunk_id }}</span>
            <el-tag v-if="isParent(s)" size="small" type="info" effect="plain">父分段 · 不参与向量检索</el-tag>
            <el-tag v-else-if="s.status === 'VECTOR_STORED'" size="small" type="success" effect="light">已向量化</el-tag>
            <el-tag v-else size="small" type="warning" effect="light">未向量化</el-tag>
          </div>

          <pre class="ci__text">{{ s.text }}</pre>

          <div class="ci__meta">
            <span v-if="s.parent_chunk_id" class="ke-muted">父分段：{{ s.parent_chunk_id }}</span>
            <span v-if="s.brother_chunk_id" class="ke-muted">兄弟分段：{{ s.brother_chunk_id }}</span>
            <span v-if="s.embedding_dim" class="ke-muted">
              向量：{{ s.embedding_dim }} 维 · L2 模长 {{ s.vector_norm }}
            </span>
          </div>

          <div v-if="s.vector_preview.length" class="ci__vec">
            <span v-for="(v, i) in s.vector_preview" :key="i" class="ci__bar-cell"
                  :title="`第 ${i + 1} 维：${v}`">
              <span class="ci__bar-track">
                <span class="ci__bar" :class="barClass(v)" :style="{ width: barWidth(v) }" />
              </span>
              <span class="ci__bar-value">{{ v }}</span>
            </span>
          </div>
          <p v-else-if="isParent(s)" class="ci__hint ke-muted">
            Markdown 父分段只保留全文，命中其子分段时回溯替换为父分段，因此不生成向量。
          </p>
        </div>
      </div>

      <div v-if="data && data.total > size" class="ci__pager">
        <el-pagination layout="prev, pager, next, total" :total="data.total"
                       :page-size="size" v-model:current-page="page" background small />
      </div>
    </div>
  </el-dialog>
</template>

<style scoped lang="scss">
.ci {
  &__stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--ke-space-3); margin-bottom: var(--ke-space-3); }
  &__stat {
    padding: var(--ke-space-2) var(--ke-space-3); border: 1px solid var(--ke-gray-200);
    border-radius: var(--ke-radius-md); background: var(--ke-gray-50, #f8fafc);
    display: flex; flex-direction: column; gap: 2px;
  }
  &__stat-label { font-size: 12px; color: var(--ke-gray-500); }
  &__stat-value { font-size: 20px; font-weight: 600; color: var(--ke-brand-600, #2563eb); }
  &__list { display: flex; flex-direction: column; gap: var(--ke-space-3); max-height: 64vh; overflow: auto; padding-right: 4px; }
  &__item { border: 1px solid var(--ke-gray-200); border-radius: var(--ke-radius-md); padding: var(--ke-space-3); }
  &__head { display: flex; align-items: center; gap: var(--ke-space-2); flex-wrap: wrap; }
  &__order { font-weight: 600; color: var(--ke-gray-800); font-size: 13px; }
  &__chunk { font-size: 12px; color: var(--ke-gray-400); font-family: monospace; }
  &__text {
    margin: var(--ke-space-2) 0 0; padding: var(--ke-space-2) var(--ke-space-3);
    background: var(--ke-gray-50, #f8fafc); border-radius: var(--ke-radius-sm);
    white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.6;
    max-height: 220px; overflow: auto; color: var(--ke-gray-700);
  }
  &__meta { display: flex; gap: var(--ke-space-4); flex-wrap: wrap; margin-top: var(--ke-space-2); font-size: 12px; }
  &__vec { display: flex; gap: 6px; flex-wrap: wrap; margin-top: var(--ke-space-2); }
  &__bar-cell { display: flex; flex-direction: column; align-items: center; gap: 2px; width: 52px; }
  &__bar-track { position: relative; width: 100%; height: 4px; background: var(--ke-gray-200); border-radius: 2px; overflow: hidden; }
  &__bar { position: absolute; top: 0; height: 100%; border-radius: 2px; }
  &__bar.pos { left: 50%; background: #2563eb; }
  &__bar.neg { right: 50%; background: #ef4444; }
  &__bar-value { font-size: 10px; color: var(--ke-gray-500); font-family: monospace; }
  &__hint { margin: var(--ke-space-2) 0 0; font-size: 12px; }
  &__pager { display: flex; justify-content: flex-end; margin-top: var(--ke-space-3); }
}
</style>
