import { onBeforeUnmount, onMounted, ref } from 'vue'

/**
 * 窄屏断点监听：默认 768px，与全局样式中的媒体查询保持一致。
 * 表格类页面需要切换渲染结构（桌面表格 / 移动端卡片）时使用。
 */
export function useBreakpoint(maxWidth = 768) {
  const isMobile = ref(false)
  let mql: MediaQueryList | null = null

  function sync(event?: MediaQueryListEvent): void {
    isMobile.value = event ? event.matches : Boolean(mql?.matches)
  }

  onMounted(() => {
    mql = window.matchMedia(`(max-width: ${maxWidth}px)`)
    sync()
    mql.addEventListener('change', sync)
  })

  onBeforeUnmount(() => {
    mql?.removeEventListener('change', sync)
  })

  return { isMobile }
}
