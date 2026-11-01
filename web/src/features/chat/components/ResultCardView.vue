<script setup lang="ts">
// 结构化结果卡片：Text2SQL / Text2Cypher 产出的对比表
import { computed } from 'vue'
import type { ResultCard } from '@/types/chat'

const props = defineProps<{ card: ResultCard }>()

// 单元格值统一转字符串展示，规避对象/ null 直渲染
function cellText(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

const hasData = computed(() => props.card.data.length > 0)

// 列名兜底：后端已推导，此处再防一手 LLM 卡缺 columns 时按首行键推导，避免零列空表
const columns = computed<string[]>(() => {
  const c = props.card.columns
  if (c && c.length) return c
  const first = props.card.data[0]
  return first ? Object.keys(first) : []
})
</script>

<template>
  <section class="rc" :aria-label="card.title || '查询结果'">
    <header v-if="card.title" class="rc__head">
      <el-icon :size="15"><Grid /></el-icon>
      <span class="rc__title ke-truncate">{{ card.title }}</span>
    </header>

    <div class="rc__scroll">
      <el-table v-if="hasData" :data="card.data" size="small" border
                max-height="320" class="rc__table">
        <el-table-column
          v-for="col in columns"
          :key="col"
          :label="col"
          :prop="col"
          min-width="120"
          show-overflow-tooltip>
          <template #default="{ row }">{{ cellText(row[col]) }}</template>
        </el-table-column>
      </el-table>
      <p v-else class="rc__empty ke-muted">未查询到匹配数据</p>
    </div>
  </section>
</template>

<style scoped lang="scss">
.rc {
  margin-top: var(--ke-space-3);
  border: 1px solid var(--ke-gray-200);
  border-radius: var(--ke-radius-md);
  background: var(--ke-gray-25);
  overflow: hidden;

  &__head {
    display: flex;
    align-items: center;
    gap: var(--ke-space-2);
    padding: var(--ke-space-2) var(--ke-space-3);
    border-bottom: 1px solid var(--ke-gray-200);
    color: var(--ke-brand-600);
  }
  &__title { font-size: 13px; font-weight: 600; }
  &__scroll { padding: var(--ke-space-2); overflow-x: auto; }
  &__table { width: 100%; }
  &__empty { margin: 0; padding: var(--ke-space-4); text-align: center; font-size: 13px; }
}
</style>
