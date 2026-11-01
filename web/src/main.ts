import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

import 'element-plus/dist/index.css'
import '@/styles/index.scss'

import App from './App.vue'
import router from './router'
import { useAuthStore } from '@/stores/auth'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(ElementPlus, { locale: zhCn })

// 全局注册 Element Plus 图标组件，模板中可直接 <el-icon><Search /></el-icon>
for (const [name, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, component)
}

// 首屏前先用本地令牌恢复身份，再安装路由：
// 否则冷启动深链接管理员路由时，守卫会在身份未就绪（isAdmin=false）时误跳对话页。
async function bootstrap(): Promise<void> {
  await useAuthStore().loadMe()
  app.use(router)
  await router.isReady()
  app.mount('#app')
}

bootstrap()
