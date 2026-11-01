/// <reference types="vite/client" />

// 单文件组件的类型垫片（供 vue-tsc 识别 .vue 模块）
declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

// 自定义环境变量：与 .env.* 中 VITE_ 前缀字段一一对应
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string
  readonly VITE_APP_TITLE: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
