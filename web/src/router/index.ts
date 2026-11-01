import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/features/auth/UserLoginView.vue'),
    meta: { title: '普通用户登录', guestOnly: true },
  },
  {
    path: '/admin-login',
    name: 'admin-login',
    component: () => import('@/features/auth/AdminLoginView.vue'),
    meta: { title: '管理员登录', guestOnly: true },
  },
  {
    path: '/',
    component: () => import('@/components/layout/AppShell.vue'),
    meta: { requiresAuth: true },
    children: [
      { path: '', redirect: { name: 'chat' } },
      {
        path: 'chat/:conversationId?',
        name: 'chat',
        component: () => import('@/features/chat/ChatView.vue'),
        meta: { title: '智能问答', requiresAuth: true },
      },
      {
        path: 'documents',
        name: 'documents',
        component: () => import('@/features/document/DocumentListView.vue'),
        meta: { title: '知识文档', requiresAuth: true, requiresAdmin: true },
      },
      {
        path: 'qa-cache',
        name: 'qa-cache',
        component: () => import('@/features/qa-cache/QaCacheReviewView.vue'),
        meta: { title: '问答审核', requiresAuth: true, requiresAdmin: true },
      },
      {
        path: 'models',
        name: 'models',
        component: () => import('@/features/admin/ModelConfigView.vue'),
        meta: { title: '模型配置', requiresAuth: true, requiresAdmin: true },
      },
      {
        path: 'users',
        name: 'users',
        component: () => import('@/features/admin/UserManageView.vue'),
        meta: { title: '用户管理', requiresAuth: true, requiresAdmin: true },
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/components/common/NotFoundView.vue'),
    meta: { title: '页面不存在' },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  document.title = `${String(to.meta.title ?? 'KnowEngine')} · KnowEngine`

  // 需登录而未登录 → 跳用户登录并记住来路
  if (to.meta.requiresAuth && !auth.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  // 已登录访问登录页 → 直接进主区
  if (to.meta.guestOnly && auth.isAuthenticated) {
    return { name: 'chat' }
  }
  // 需管理员权限而非管理员 → 回对话页（越权不落空白）
  if (to.meta.requiresAdmin && auth.isAuthenticated && !auth.isAdmin) {
    return { name: 'chat' }
  }
  return true
})

export default router
