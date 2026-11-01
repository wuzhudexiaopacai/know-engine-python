import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      port: 5173,
      // 开发态把 /api 代理到本地后端，规避跨域；生产由 FastAPI 同源托管，无需代理
      proxy: {
        '/api': {
          target: env.VITE_API_TARGET || 'http://127.0.0.1:8009',
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: 'dist',
      // 分包：把体积较大的 UI 框架与工具库单独切出，利用长效缓存
      chunkSizeWarningLimit: 1200,
      rollupOptions: {
        output: {
          manualChunks: {
            vue: ['vue', 'vue-router', 'pinia'],
            ui: ['element-plus', '@element-plus/icons-vue'],
          },
        },
      },
    },
  }
})
