<script setup lang="ts">
// 会话列表：嵌入应用侧边栏（导航下方），负责展示与删除，切换/新建由外层触发
import { computed } from 'vue'
import type { Conversation } from '@/types/chat'
import { formatDateTime } from '@/utils/format'

const props = defineProps<{
  conversations: Conversation[]
  activeId: string | null
  loading: boolean
  /** 管理员查看全部用户会话时，在条目上标注归属 */
  viewAll?: boolean
}>()

const emit = defineEmits<{
  (e: 'select', id: string): void
  (e: 'remove', id: string): void
}>()

const isEmpty = computed(() => !props.loading && props.conversations.length === 0)

/** 归属键 identity:sub → 可读标签（管理员查看全部会话时区分归属） */
function ownerLabel(owner?: string): string {
  if (!owner) return ''
  const [identity, id] = owner.split(':')
  // 改造前的历史会话只存裸自增 id，归属无法判定
  if (!id) return `未归属 #${owner}`
  return identity === 'admin' ? `管理员 #${id}` : `用户 #${id}`
}
</script>

<template>
  <div class="conv" role="list">
    <el-skeleton v-if="loading" :rows="4" animated class="conv__skeleton" />

    <p v-else-if="isEmpty" class="conv__empty ke-muted">暂无历史会话</p>

    <ul v-else class="conv__list">
      <li
        v-for="(c, i) in conversations"
        :key="c.conversation_id"
        class="conv__item-wrap"
        role="listitem"
        :style="{ animationDelay: Math.min(i, 10) * 0.03 + 's' }"
      >
        <button
          class="conv__item"
          :class="{ 'is-active': c.conversation_id === activeId }"
          :aria-current="c.conversation_id === activeId ? 'true' : undefined"
          @click="emit('select', c.conversation_id)"
        >
          <span class="conv__title ke-truncate">{{ c.title || '未命名会话' }}</span>
          <span class="conv__meta">
            <span v-if="viewAll && c.owner" class="conv__owner ke-truncate">{{ ownerLabel(c.owner) }}</span>
            <span class="conv__time">{{ formatDateTime(c.created_at) }}</span>
          </span>
        </button>
        <button class="conv__del" aria-label="删除会话" @click.stop="emit('remove', c.conversation_id)">
          <el-icon :size="14"><Delete /></el-icon>
        </button>
      </li>
    </ul>
  </div>
</template>

<style scoped lang="scss">
// 侧栏内的会话条目：单行标题 + 次要信息，密度接近豆包/Codex 的历史列表
.conv {
  display: flex;
  flex-direction: column;
  min-height: 0;

  &__skeleton { padding: 4px 6px; }
  &__empty {
    margin: 0;
    padding: 20px 10px;
    text-align: center;
    font-size: 12.5px;
  }

  &__list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  &__item-wrap {
    position: relative;
    display: flex;
    align-items: center;
    animation: ke-enter-up 0.24s var(--ke-ease-out) both;
  }

  &__item {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 7px 28px 7px 10px;
    border: 1px solid transparent;
    border-radius: var(--ke-radius-md);
    background: transparent;
    text-align: left;
    cursor: pointer;
    transition: background var(--ke-duration-fast), border-color var(--ke-duration-fast);

    &:hover { background: var(--ke-bg-hover); }
    &:focus-visible { outline: 2px solid var(--ke-brand-400); outline-offset: 1px; }
    &.is-active {
      background: var(--ke-bg-active);
      border-color: var(--ke-brand-100);
      .conv__title { color: var(--ke-brand-700); font-weight: 600; }
    }
  }
  &__title {
    font-size: 13px;
    line-height: 1.35;
    color: var(--ke-gray-700);
  }
  &__meta {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    font-size: 11px;
    color: var(--ke-gray-400);
  }
  &__owner {
    flex: 0 1 auto;
    min-width: 0;
    color: var(--ke-brand-500);
  }
  &__time { flex: 0 0 auto; }

  &__del {
    position: absolute;
    right: 4px;
    top: 50%;
    transform: translateY(-50%);
    width: 24px;
    height: 24px;
    display: grid;
    place-items: center;
    padding: 0;
    border: none;
    border-radius: var(--ke-radius-sm);
    background: transparent;
    color: var(--ke-gray-400);
    cursor: pointer;
    opacity: 0;
    transition: opacity var(--ke-duration-fast), color var(--ke-duration-fast),
                background var(--ke-duration-fast);
  }
  &__item-wrap:hover &__del,
  &__del:focus-visible { opacity: 1; }
  &__del:hover { color: var(--ke-danger); background: var(--ke-gray-100); }
}

/* 触屏没有 hover，删除入口常驻显示 */
@media (max-width: 768px) {
  .conv__del { opacity: 1; }
}
</style>
