<script setup lang="ts">
// 会话列表侧栏：新建、切换、删除历史会话
import { computed } from 'vue'
import type { Conversation } from '@/types/chat'
import { formatDateTime } from '@/utils/format'

const props = defineProps<{
  conversations: Conversation[]
  activeId: string | null
  loading: boolean
  /** 管理员可见：是否展示“全部用户会话”开关 */
  canViewAll?: boolean
  /** 当前是否查看全部用户会话 */
  viewAll?: boolean
}>()

const emit = defineEmits<{
  (e: 'select', id: string): void
  (e: 'create'): void
  (e: 'remove', id: string): void
  (e: 'toggle-all', value: boolean): void
}>()

const isEmpty = computed(() => !props.loading && props.conversations.length === 0)

/** 归属键 identity:sub → 可读标签（管理员查看全部会话时区分归属） */
function ownerLabel(owner?: string): string {
  if (!owner) return ''
  const [identity, id] = owner.split(':')
  // 改造前的历史会话只存裸自增 id，归属无法判定
  if (!id) return `未归属 #${owner}`
  return identity === 'admin' ? `管理员 #${id}` : `普通用户 #${id}`
}

function onRemove(id: string): void {
  emit('remove', id)
}
</script>

<template>
  <aside class="conv" aria-label="历史会话">
    <div class="conv__head">
      <span class="conv__head-title">会话</span>
      <el-button type="primary" size="small" :icon="'Plus'" @click="emit('create')">
        新建对话
      </el-button>
    </div>

    <div v-if="canViewAll" class="conv__scope">
      <span class="conv__scope-label">查看全部用户会话</span>
      <el-switch
        :model-value="viewAll"
        size="small"
        @update:model-value="emit('toggle-all', $event as boolean)"
      />
    </div>

    <div class="conv__scroll" role="list">
      <el-skeleton v-if="loading" :rows="5" animated class="conv__skeleton" />

      <p v-else-if="isEmpty" class="conv__empty ke-muted">
        暂无历史会话，开始你的第一次提问
      </p>

      <ul v-else class="conv__list">
        <li v-for="(c, i) in conversations" :key="c.conversation_id" class="conv__item-wrap" role="listitem"
            :style="{ animationDelay: Math.min(i, 10) * 0.03 + 's' }">
          <button
            class="conv__item"
            :class="{ 'is-active': c.conversation_id === activeId }"
            :aria-current="c.conversation_id === activeId ? 'true' : undefined"
            @click="emit('select', c.conversation_id)">
            <span class="conv__title ke-truncate">{{ c.title || '未命名会话' }}</span>
            <span v-if="viewAll && c.owner" class="conv__owner">{{ ownerLabel(c.owner) }}</span>
            <span class="conv__time">{{ formatDateTime(c.created_at) }}</span>
          </button>
          <button class="conv__del" aria-label="删除会话" @click.stop="onRemove(c.conversation_id)">
            <el-icon :size="14"><Delete /></el-icon>
          </button>
        </li>
      </ul>
    </div>
  </aside>
</template>

<style scoped lang="scss">
.conv {
  width: var(--ke-sidebar-w);
  flex: 0 0 auto;
  display: flex; flex-direction: column;
  background: var(--ke-bg-sidebar); border-right: 1px solid var(--ke-brand-100);
  min-height: 0;

  &__head {
    display: flex; align-items: center; justify-content: space-between;
    padding: var(--ke-space-4);
    border-bottom: 1px solid var(--ke-brand-100);
  }
  &__head-title { font-size: 14px; font-weight: 600; color: var(--ke-gray-800); }

  &__scroll { flex: 1; min-height: 0; overflow-y: auto; padding: var(--ke-space-2) var(--ke-space-3); }

  &__scope {
    display: flex; align-items: center; justify-content: space-between;
    padding: var(--ke-space-3) var(--ke-space-5);
    border-bottom: 1px solid var(--ke-brand-100);
  }
  &__scope-label { font-size: 12px; color: var(--ke-gray-500); }
  &__skeleton { padding: var(--ke-space-3); }
  &__empty { padding: var(--ke-space-6) var(--ke-space-4); text-align: center; font-size: 13px; }

  &__list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  &__item-wrap {
    position: relative; display: flex; align-items: center;
  }

  &__item {
    position: relative;
    flex: 1; min-width: 0; min-height: 52px; text-align: left;
    display: flex; flex-direction: column; justify-content: center; gap: 2px;
    padding: 10px 12px;
    border: 1px solid transparent;
    border-radius: var(--ke-radius-sm);
    background: transparent; cursor: pointer; overflow: hidden;
    transition: background var(--ke-duration-base), border-color var(--ke-duration-base),
                color var(--ke-duration-base);
    // 悬停：浅蓝底过渡，不改变布局
    &:hover { background: var(--ke-brand-50); border-color: var(--ke-brand-100); }
    &:active { transform: scale(0.995); }
    // 选中：柔和蓝色底 + 文字高亮
    &.is-active {
      background: var(--ke-bg-active);
      border-color: var(--ke-brand-200);
      .conv__title { color: var(--ke-brand-700); font-weight: 600; }
      .conv__time { color: var(--ke-brand-600); }
    }
  }
  &__title { font-size: 14px; color: var(--ke-gray-700); }
  &__owner { font-size: 11px; color: var(--ke-brand-600); }
  &__time { font-size: 11px; color: var(--ke-gray-400); }

  &__del {
    flex: 0 0 auto; margin-left: 4px; width: 26px; height: 26px;
    display: grid; place-items: center;
    border: none; border-radius: var(--ke-radius-sm); background: transparent;
    color: var(--ke-gray-400); cursor: pointer; opacity: 0; transition: opacity 0.15s, color 0.15s;
  }
  &__item-wrap:hover &__del, &__del:focus-visible { opacity: 1; }
  &__del:hover { color: var(--ke-danger); background: var(--ke-gray-100); }
}

@media (max-width: 720px) {
  .conv { position: absolute; z-index: 5; height: 100%; box-shadow: var(--ke-shadow-md); }
}
</style>
