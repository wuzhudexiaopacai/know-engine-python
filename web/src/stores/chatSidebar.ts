import { defineStore } from 'pinia'
import { ref, shallowRef } from 'vue'
import type { Conversation } from '@/types/chat'

/** 会话操作入口：由对话页注册，侧栏只负责触发，不承载对话内部状态 */
interface SidebarHandlers {
  select: (id: string) => void
  create: () => void
  remove: (id: string) => void
  toggleAll: (value: boolean) => void
}

/**
 * 会话侧栏共享态：会话列表统一挂在应用侧边栏（导航下方），
 * 数据由对话页写入、布局由 AppShell 渲染，两者通过本 store 解耦。
 */
export const useChatSidebarStore = defineStore('chatSidebar', () => {
  const conversations = ref<Conversation[]>([])
  const activeId = ref<string | null>(null)
  const loading = ref(false)
  const viewAll = ref(false)
  const handlers = shallowRef<Partial<SidebarHandlers>>({})

  /** 对话页挂载时注册操作；卸载时清空，避免侧栏触发已销毁页面的逻辑 */
  function bind(next: Partial<SidebarHandlers>): void {
    handlers.value = next
  }

  function unbind(): void {
    handlers.value = {}
  }

  function select(id: string): void {
    handlers.value.select?.(id)
  }

  function create(): void {
    handlers.value.create?.()
  }

  function remove(id: string): void {
    handlers.value.remove?.(id)
  }

  function toggleAll(value: boolean): void {
    handlers.value.toggleAll?.(value)
  }

  return {
    conversations, activeId, loading, viewAll,
    bind, unbind, select, create, remove, toggleAll,
  }
})
