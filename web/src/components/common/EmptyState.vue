<script setup lang="ts">
// 通用空态/异常态占位：列表无数据、检索失败、无权限等场景统一兜底，杜绝空白页。
import { computed } from 'vue'

type Variant = 'empty' | 'error' | 'forbidden' | 'no-result'

const props = withDefaults(
  defineProps<{
    variant?: Variant
    title?: string
    description?: string
    /** 是否显示重试按钮（error 场景常用） */
    showRetry?: boolean
  }>(),
  {
    variant: 'empty',
    showRetry: false,
  },
)

const emit = defineEmits<{ retry: [] }>()

const DEFAULTS: Record<Variant, { title: string; desc: string; icon: string }> = {
  empty: { title: '暂无数据', desc: '这里还没有内容', icon: 'Box' },
  'no-result': { title: '没有匹配结果', desc: '换个关键词或条件试试', icon: 'Search' },
  error: { title: '加载失败', desc: '服务暂时不可用，请稍后重试', icon: 'WarningFilled' },
  forbidden: { title: '权限不足', desc: '当前账号无权访问该功能', icon: 'Lock' },
}

const info = computed(() => DEFAULTS[props.variant])
const titleText = computed(() => props.title ?? info.value.title)
const descText = computed(() => props.description ?? info.value.desc)
</script>

<template>
  <section class="ke-empty" role="status" aria-live="polite">
    <el-icon class="ke-empty__icon" :size="46">
      <component :is="info.icon" />
    </el-icon>
    <h3 class="ke-empty__title">{{ titleText }}</h3>
    <p class="ke-empty__desc">{{ descText }}</p>
    <el-button v-if="showRetry" type="primary" plain @click="emit('retry')">重新加载</el-button>
    <slot />
  </section>
</template>

<style scoped lang="scss">
.ke-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--ke-space-2);
  padding: var(--ke-space-8) var(--ke-space-4);
  text-align: center;

  &__icon {
    color: var(--ke-gray-300);
  }
  &__title {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    color: var(--ke-gray-700);
  }
  &__desc {
    margin: 0;
    font-size: 13px;
    color: var(--ke-gray-500);
  }
}
</style>
