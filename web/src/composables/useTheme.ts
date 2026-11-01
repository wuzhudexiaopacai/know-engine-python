// 主题切换：Obsidian 暗色为默认，可切亮色。
// 通过在 <html> 上增删 `dark` 类实现，与 Element Plus 官方 dark css-vars 约定一致。
import { ref } from 'vue'

type Theme = 'dark' | 'light'

const STORAGE_KEY = 'ke-theme'
const DEFAULT_THEME: Theme = 'dark'

// 模块级单例状态，跨组件共享当前主题。
const isDark = ref(readStored() === 'dark')

function readStored(): Theme {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    return v === 'light' || v === 'dark' ? v : DEFAULT_THEME
  } catch {
    return DEFAULT_THEME
  }
}

function applyTheme(dark: boolean): void {
  const el = document.documentElement
  el.classList.toggle('dark', dark)
  el.style.colorScheme = dark ? 'dark' : 'light'
}

/** 全局主题控制器（单例）。 */
export function useTheme() {
  function setTheme(theme: Theme): void {
    isDark.value = theme === 'dark'
    try {
      localStorage.setItem(STORAGE_KEY, theme)
    } catch {
      /* 隐私模式等场景忽略持久化失败 */
    }
    applyTheme(isDark.value)
  }

  function toggle(): void {
    setTheme(isDark.value ? 'light' : 'dark')
  }

  /** 应用启动时调用：按存储值立即应用，避免首屏闪白。 */
  function init(): void {
    applyTheme(isDark.value)
  }

  return { isDark, setTheme, toggle, init }
}
