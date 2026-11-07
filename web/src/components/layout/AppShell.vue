<script setup lang="ts">
// 应用外壳：左侧导航栏 + 内容画布；移动端导航收进抽屉，保持与桌面一致的侧边栏体验。
import { computed, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type UploadRequestOptions } from 'element-plus'
import { fetchProfile, updateProfile, uploadAvatar } from '@/api/auth'
import { getChatPreferences, updateChatPreferences } from '@/api/chat'
import { useAuthStore } from '@/stores/auth'
import { useChatSidebarStore } from '@/stores/chatSidebar'
import { useTheme } from '@/composables/useTheme'
import ConversationList from '@/features/chat/components/ConversationList.vue'
import type { ProfileUpdatePayload } from '@/types/auth'

const auth = useAuthStore()
const { isDark, toggle: toggleTheme } = useTheme()
const route = useRoute()
const router = useRouter()

interface NavItem {
  name: string
  label: string
  icon: string
  /** 访问所需 RBAC 权限点，未配置则所有已登录用户可见 */
  permission?: string
}

const NAV: NavItem[] = [
  { name: 'chat', label: '智能问答', icon: 'ChatDotRound' },
  { name: 'documents', label: '知识文档', icon: 'Document', permission: 'document:read' },
  { name: 'qa-cache', label: '问答审核', icon: 'Stamp', permission: 'qa:review' },
  { name: 'models', label: '模型配置', icon: 'Cpu', permission: 'model:manage' },
  { name: 'users', label: '成员管理', icon: 'User', permission: 'member:manage' },
  { name: 'tenants', label: '租户管理', icon: 'OfficeBuilding', permission: 'tenant:manage' },
  { name: 'logs', label: '日志中心', icon: 'Tickets', permission: 'log:view' },
]

const visibleNav = computed(() => NAV.filter((n) => !n.permission || auth.can(n.permission)))
const activeName = computed(() => String(route.name ?? 'chat'))
const currentLabel = computed(
  () => NAV.find((n) => n.name === activeName.value)?.label ?? '工作台',
)

// 对话页把历史会话挂在导航下方，桌面/移动端共用同一条侧边栏
const chatSidebar = useChatSidebarStore()
const isChat = computed(() => activeName.value === 'chat')
const canAuditChat = computed(() => auth.can('chat:audit'))

// 移动端侧边栏：汉堡按钮展开，选中菜单 / 遮罩 关闭
const mobileOpen = ref(false)

function go(name: string): void {
  mobileOpen.value = false
  if (route.name !== name) router.push({ name })
}

function onSidebarSelect(id: string): void {
  mobileOpen.value = false
  chatSidebar.select(id)
}

function onSidebarCreate(): void {
  mobileOpen.value = false
  chatSidebar.create()
}

watch(activeName, () => {
  mobileOpen.value = false
})

async function onLogout(): Promise<void> {
  try {
    await ElMessageBox.confirm('确定退出登录吗？', '退出确认', {
      type: 'warning',
      confirmButtonText: '退出',
      cancelButtonText: '取消',
    })
    auth.logout()
    router.replace({ name: 'login' })
  } catch {
    /* 用户取消 */
  }
}

const initial = computed(() => auth.displayName.charAt(0) || 'U')

// ---------------- 个人资料（头像 + 座右铭） ----------------
const profileOpen = ref(false)
const profileLoading = ref(false)
const profileSaving = ref(false)
const avatarUploading = ref(false)
const profile = reactive({
  name: '',
  nickname: '',
  motto: '',
  avatar: '',
  lockVersion: 0,
})

/** 长期记忆开关：用户偏好，关闭后不生成也不注入对话摘要 */
const longTermMemory = ref(true)

async function openProfile(): Promise<void> {
  profileOpen.value = true
  profileLoading.value = true
  try {
    const [p, pref] = await Promise.all([fetchProfile(), getChatPreferences()])
    profile.name = p.name ?? ''
    profile.nickname = p.nickname ?? ''
    profile.motto = p.motto ?? ''
    profile.avatar = p.avatar ?? ''
    profile.lockVersion = p.lock_version
    longTermMemory.value = pref.long_term_memory_enabled
  } catch {
    /* 拦截器已提示 */
  } finally {
    profileLoading.value = false
  }
}

/** 切换长期记忆：乐观更新 + 立即持久化，失败回滚 */
async function onToggleLongTermMemory(value: boolean | string | number): Promise<void> {
  const enabled = !!value
  if (enabled === longTermMemory.value) return // 开关自身已同步过状态时跳过
  longTermMemory.value = enabled
  try {
    await updateChatPreferences({ long_term_memory_enabled: enabled })
    ElMessage.success(enabled ? '已开启长期记忆' : '已关闭长期记忆')
  } catch {
    longTermMemory.value = !enabled // 回滚
  }
}

/** 点击设置行空白区也可切换；点在开关本体上时由开关自身处理，避免双重触发 */
function onSettingRowClick(e: MouseEvent): void {
  if (profileLoading.value) return
  if ((e.target as HTMLElement).closest('.el-switch')) return
  void onToggleLongTermMemory(!longTermMemory.value)
}

function beforeAvatarUpload(file: File): boolean {
  if (!file.type.startsWith('image/')) {
    ElMessage.warning('请选择图片文件（png/jpg/webp 等）')
    return false
  }
  if (file.size > 2 * 1024 * 1024) {
    ElMessage.warning('头像图片不能超过 2MB')
    return false
  }
  return true
}

async function handleAvatarUpload(options: UploadRequestOptions): Promise<void> {
  avatarUploading.value = true
  try {
    const res = await uploadAvatar(options.file)
    profile.avatar = res.url
    ElMessage.success('头像已上传，保存后生效')
  } catch {
    /* 拦截器已提示 */
  } finally {
    avatarUploading.value = false
  }
}

async function saveProfile(): Promise<void> {
  if (profileSaving.value) return
  profileSaving.value = true
  try {
    const payload: ProfileUpdatePayload = {
      motto: profile.motto.trim() || null,
      avatar: profile.avatar || null,
      lock_version: profile.lockVersion,
    }
    // 用户可改昵称，管理员可改姓名
    if (auth.isAdmin) payload.name = profile.name.trim() || null
    else payload.nickname = profile.nickname.trim() || null
    const p = await updateProfile(payload)
    auth.applyProfile(p)
    profile.lockVersion = p.lock_version
    ElMessage.success('资料已保存')
    profileOpen.value = false
  } catch {
    /* 拦截器已提示 */
  } finally {
    profileSaving.value = false
  }
}
</script>

<template>
  <div class="shell" :class="{ 'is-drawer-open': mobileOpen }">
    <!-- 桌面：左侧固定导航；移动端：从左侧滑出的抽屉（与桌面同一套菜单） -->
    <aside id="app-sidebar" class="shell__sidebar" aria-label="主导航">
      <div class="shell__brand" @click="go('chat')" tabindex="0"
             @keydown.enter="go('chat')" aria-label="返回智能问答">
        <span class="shell__logo" aria-hidden="true">K</span>
        <span class="shell__brand-text">
          <span class="shell__name">KnowEngine</span>
          <span class="shell__tag">企业级智能知识引擎</span>
        </span>
      </div>

      <nav class="shell__nav" :class="{ 'is-compact': isChat }">
        <button
          v-for="item in visibleNav"
          :key="item.name"
          class="shell__navitem"
          :class="{ 'is-active': activeName === item.name }"
          :data-nav="item.name"
          :aria-label="item.label"
          :aria-current="activeName === item.name ? 'page' : undefined"
          @click="go(item.name)"
        >
          <el-icon :size="18"><component :is="item.icon" /></el-icon>
          <span class="shell__navlabel">{{ item.label }}</span>
        </button>
      </nav>

      <!-- 对话页：历史会话并入侧边栏导航下方，不再单独占一列 -->
      <div v-if="isChat" class="shell__convs">
        <div class="shell__convs-head">
          <span class="shell__convs-title">会话</span>
          <button class="shell__newbtn" aria-label="新建对话" @click="onSidebarCreate">
            <el-icon :size="16"><Plus /></el-icon>
          </button>
        </div>
        <div v-if="canAuditChat" class="shell__scope">
          <span>全部用户会话</span>
          <el-switch
            :model-value="chatSidebar.viewAll"
            size="small"
            @update:model-value="chatSidebar.toggleAll($event as boolean)"
          />
        </div>
        <div class="shell__convs-scroll">
          <ConversationList
            :conversations="chatSidebar.conversations"
            :active-id="chatSidebar.activeId"
            :loading="chatSidebar.loading"
            :view-all="chatSidebar.viewAll"
            @select="onSidebarSelect"
            @remove="chatSidebar.remove"
          />
        </div>
      </div>

      <div class="shell__side-foot">
        <el-dropdown trigger="click" placement="top-start">
          <button class="shell__user" aria-label="用户菜单">
            <el-avatar :size="32" :src="auth.avatar || undefined" class="shell__avatar">{{ initial }}</el-avatar>
            <span class="shell__user-text">
              <span class="shell__username ke-truncate">{{ auth.displayName || '未登录' }}</span>
              <span class="shell__usermeta ke-truncate">
                {{ auth.tenant?.name || auth.tenantCode }} · {{ auth.isAdmin ? '管理员' : '成员' }}
              </span>
            </span>
            <el-icon class="shell__chevron" :size="14"><ArrowDown /></el-icon>
          </button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-if="auth.motto" disabled>
                <span class="shell__motto ke-truncate">{{ auth.motto }}</span>
              </el-dropdown-item>
              <el-dropdown-item @click="toggleTheme">
                <el-icon><component :is="isDark ? 'Sunny' : 'Moon'" /></el-icon>
                {{ isDark ? '亮色模式' : '暗色模式' }}
              </el-dropdown-item>
              <el-dropdown-item divided @click="openProfile">
                <el-icon><User /></el-icon>
                个人资料
              </el-dropdown-item>
              <el-dropdown-item @click="onLogout">
                <el-icon><SwitchButton /></el-icon>
                退出登录
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </aside>

    <transition name="shell-fade">
      <div v-if="mobileOpen" class="shell__scrim" aria-hidden="true" @click="mobileOpen = false" />
    </transition>

    <div class="shell__content">
      <!-- 移动端顶栏：汉堡菜单 + 当前模块 -->
      <header class="shell__mobilebar" role="banner">
        <button class="shell__iconbtn" aria-label="打开导航菜单"
                :aria-expanded="mobileOpen" aria-controls="app-sidebar"
                @click="mobileOpen = !mobileOpen">
          <el-icon :size="20"><component :is="mobileOpen ? 'Close' : 'Menu'" /></el-icon>
        </button>
        <span class="shell__mobiletitle">{{ currentLabel }}</span>
        <button class="shell__iconbtn" aria-label="个人资料" @click="openProfile">
          <el-avatar :size="28" :src="auth.avatar || undefined" class="shell__avatar">{{ initial }}</el-avatar>
        </button>
      </header>

      <main class="shell__main">
        <!-- 面包屑：非对话画布页统一展示当前模块位置 -->
        <nav v-if="activeName !== 'chat'" class="shell__crumb" aria-label="面包屑">
          <span>工作台</span>
          <span class="shell__crumb-sep">/</span>
          <span class="is-current">{{ currentLabel }}</span>
        </nav>
        <router-view v-slot="{ Component }">
          <transition name="ke-page" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </main>
    </div>

    <el-dialog v-model="profileOpen" title="个人资料" width="min(480px, 94vw)" append-to-body>
      <div v-loading="profileLoading" class="profile">
        <div class="profile__avatar">
          <el-upload
            class="profile__uploader"
            action="#"
            :show-file-list="false"
            :http-request="handleAvatarUpload"
            :before-upload="beforeAvatarUpload"
            accept="image/*"
          >
            <el-avatar :size="72" :src="profile.avatar || undefined" class="profile__preview">
              {{ (profile.nickname || profile.name || auth.displayName || 'U').charAt(0) }}
            </el-avatar>
            <span class="profile__hint">{{ avatarUploading ? '上传中…' : '点击上传头像' }}</span>
          </el-upload>
        </div>

        <el-form :model="profile" label-position="top">
          <el-form-item v-if="auth.isAdmin" label="姓名">
            <el-input v-model="profile.name" maxlength="64" placeholder="显示在右上角" />
          </el-form-item>
          <el-form-item v-else label="昵称">
            <el-input v-model="profile.nickname" maxlength="128" placeholder="显示在右上角" />
          </el-form-item>
          <el-form-item label="座右铭">
            <el-input
              v-model="profile.motto"
              type="textarea"
              :rows="2"
              maxlength="255"
              show-word-limit
              placeholder="写一句激励自己的话，可选"
            />
          </el-form-item>
        </el-form>

        <el-divider content-position="left">对话设置</el-divider>
        <div
          class="profile__setting"
          role="switch"
          :aria-checked="longTermMemory"
          :aria-disabled="profileLoading"
          tabindex="0"
          aria-label="长期记忆"
          @click="onSettingRowClick"
          @keydown.space.prevent="onToggleLongTermMemory(!longTermMemory)"
          @keydown.enter.prevent="onToggleLongTermMemory(!longTermMemory)"
        >
          <div class="profile__setting-text">
            <span class="profile__setting-label">长期记忆</span>
            <span class="profile__setting-desc">开启后将自动摘要历史对话，并在新会话中提供个性化建议</span>
          </div>
          <el-switch
            :model-value="longTermMemory"
            :disabled="profileLoading"
            tabindex="-1"
            @update:model-value="onToggleLongTermMemory"
          />
        </div>
      </div>
      <template #footer>
        <el-button :disabled="profileSaving" @click="profileOpen = false">取消</el-button>
        <el-button type="primary" :loading="profileSaving" @click="saveProfile">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.shell {
  height: 100vh;
  height: 100dvh;
  display: flex;
  background: var(--ke-bg-page);
  overflow: hidden;

  /* ---------------- 左侧导航（桌面常驻 / 移动端抽屉） ---------------- */
  &__sidebar {
    width: var(--ke-sidebar-w);
    flex: 0 0 var(--ke-sidebar-w);
    display: flex;
    flex-direction: column;
    gap: var(--ke-space-2);
    padding: var(--ke-space-4) var(--ke-space-3) var(--ke-space-3);
    background: var(--ke-bg-sidebar);
    border-right: 1px solid var(--ke-gray-100);
    z-index: 12;
  }

  &__brand {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 4px 8px 18px;
    cursor: pointer;
    user-select: none;
    border-radius: var(--ke-radius-md);
    &:focus-visible { outline: 2px solid var(--ke-brand-400); outline-offset: 2px; }
  }
  &__logo {
    width: 34px;
    height: 34px;
    flex: 0 0 34px;
    border-radius: 10px;
    background: linear-gradient(135deg, var(--ke-brand-500), var(--ke-brand-400));
    color: var(--ke-on-brand);
    font-weight: 700;
    display: grid;
    place-items: center;
    font-size: 16px;
    box-shadow: var(--ke-shadow-brand);
  }
  &__brand-text {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  &__name {
    font-weight: 650;
    font-size: 15px;
    color: var(--ke-gray-900);
    letter-spacing: 0.2px;
    line-height: 1.2;
  }
  &__tag {
    margin-top: 3px;
    font-size: 11px;
    color: var(--ke-gray-400);
    letter-spacing: 0.2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  &__nav {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 2px;
    scrollbar-width: thin;
  }
  &__navitem {
    position: relative;
    display: flex;
    align-items: center;
    gap: 10px;
    height: 42px;
    padding: 0 12px;
    border: none;
    border-radius: var(--ke-radius-md);
    background: transparent;
    color: var(--ke-gray-600);
    font-size: 14px;
    font-weight: 500;
    text-align: left;
    cursor: pointer;
    transition: background var(--ke-duration-fast), color var(--ke-duration-fast);

    .el-icon { flex: 0 0 auto; }
    &:hover { background: var(--ke-bg-hover); color: var(--ke-gray-900); }
    &:focus-visible { outline: 2px solid var(--ke-brand-400); outline-offset: 1px; }
    &:active { transform: scale(0.985); }
    &.is-active {
      background: var(--ke-bg-active);
      color: var(--ke-brand-600);
      font-weight: 600;
    }
    // 选中项左侧强调条：克制的品牌色，避免整块高饱和底色
    &.is-active::before {
      content: '';
      position: absolute;
      left: 0;
      top: 50%;
      width: 3px;
      height: 18px;
      border-radius: 0 3px 3px 0;
      background: var(--ke-brand-500);
      transform: translateY(-50%);
    }
  }
  &__navlabel {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* ---------------- 对话页：导航下方的会话列表 ---------------- */
  &__nav.is-compact {
    flex: 0 1 auto;
    max-height: 46%;
  }
  &__convs {
    flex: 1 1 auto;
    min-height: 96px;
    display: flex;
    flex-direction: column;
    margin-top: var(--ke-space-2);
    padding-top: var(--ke-space-2);
    border-top: 1px solid var(--ke-gray-100);
  }
  &__convs-head {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 6px 6px 10px;
  }
  &__convs-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--ke-gray-400);
    letter-spacing: 0.3px;
  }
  &__newbtn {
    width: 28px;
    height: 28px;
    display: grid;
    place-items: center;
    padding: 0;
    border: none;
    border-radius: var(--ke-radius-sm);
    background: transparent;
    color: var(--ke-gray-500);
    cursor: pointer;
    transition: background var(--ke-duration-fast), color var(--ke-duration-fast),
                transform var(--ke-duration-fast);
    &:hover { background: var(--ke-bg-active); color: var(--ke-brand-600); }
    &:active { transform: scale(0.94); }
    &:focus-visible { outline: 2px solid var(--ke-brand-400); outline-offset: 1px; }
  }
  &__scope {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--ke-space-2);
    padding: 0 10px 8px;
    font-size: 11.5px;
    color: var(--ke-gray-400);
  }
  &__convs-scroll {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 0 2px;
    scrollbar-width: thin;
  }

  &__side-foot {
    flex: 0 0 auto;
    padding-top: var(--ke-space-3);
    border-top: 1px solid var(--ke-gray-100);
  }
  &__user {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px;
    border: none;
    border-radius: var(--ke-radius-md);
    background: transparent;
    cursor: pointer;
    text-align: left;
    transition: background var(--ke-duration-fast);
    &:hover { background: var(--ke-bg-hover); }
    &:focus-visible { outline: 2px solid var(--ke-brand-400); outline-offset: 1px; }
  }
  &__avatar {
    flex: 0 0 auto;
    background: var(--ke-brand-500);
    color: var(--ke-on-brand);
    font-weight: 600;
  }
  &__user-text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  &__username {
    font-size: 13.5px;
    font-weight: 600;
    color: var(--ke-gray-800);
  }
  &__usermeta {
    font-size: 11.5px;
    color: var(--ke-gray-400);
  }
  &__chevron { color: var(--ke-gray-400); }
  &__motto {
    display: inline-block;
    max-width: 220px;
    font-size: 12px;
    font-style: italic;
    color: var(--ke-gray-400);
  }

  /* ---------------- 内容画布 ---------------- */
  &__content {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  &__mobilebar { display: none; }
  &__main {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
  }

  &__crumb {
    max-width: var(--ke-content-maxw);
    margin: 0 auto;
    padding: var(--ke-space-3) var(--ke-space-6) 0;
    font-size: 12px;
    color: var(--ke-gray-400);
  }
  &__crumb-sep { margin: 0 2px; color: var(--ke-gray-300); }
  &__crumb .is-current { color: var(--ke-gray-600); }

  &__scrim { display: none; }

  /* ---------------- H5：侧边栏变抽屉，保持桌面同款层级 ---------------- */
  @media (max-width: 768px) {
    &__sidebar {
      position: fixed;
      top: 0;
      bottom: 0;
      left: 0;
      width: min(82vw, 300px);
      flex-basis: auto;
      padding-top: calc(var(--ke-space-4) + env(safe-area-inset-top));
      transform: translateX(-104%);
      transition: transform var(--ke-duration-base) var(--ke-ease-out);
      box-shadow: var(--ke-shadow-lg);
      z-index: 30;
    }
    // 抽屉打开态
    &.is-drawer-open {
      .shell__sidebar { transform: translateX(0); }
    }

    &__scrim {
      display: block;
      position: fixed;
      inset: 0;
      background: rgba(8, 12, 20, 0.42);
      backdrop-filter: blur(2px);
      z-index: 25;
    }

    &__mobilebar {
      flex: 0 0 auto;
      display: flex;
      align-items: center;
      gap: 6px;
      height: calc(54px + env(safe-area-inset-top));
      padding: env(safe-area-inset-top) 10px 0;
      background: var(--ke-bg-page);
      border-bottom: 1px solid var(--ke-gray-100);
      z-index: 20;
    }
    &__iconbtn {
      width: 36px;
      height: 36px;
      flex: 0 0 36px;
      display: grid;
      place-items: center;
      border: none;
      border-radius: 10px;
      background: transparent;
      color: var(--ke-gray-600);
      cursor: pointer;
      transition: background var(--ke-duration-fast);
      &:hover { background: var(--ke-bg-hover); }
      &:focus-visible { outline: 2px solid var(--ke-brand-400); outline-offset: 1px; }
    }
    &__mobiletitle {
      flex: 1;
      min-width: 0;
      text-align: center;
      font-size: 14px;
      font-weight: 600;
      color: var(--ke-gray-800);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    &__main { overscroll-behavior-y: contain; }
    &__crumb { padding: 10px 14px 0; }
  }
}

// 抽屉遮罩淡入淡出
.shell-fade-enter-active,
.shell-fade-leave-active {
  transition: opacity var(--ke-duration-base) ease;
}
.shell-fade-enter-from,
.shell-fade-leave-to {
  opacity: 0;
}

// 个人资料弹窗：头像上传区居中 + 提示文案
.profile {
  display: flex;
  flex-direction: column;
  min-height: 120px;

  &__avatar {
    display: flex;
    justify-content: center;
    margin-bottom: var(--ke-space-4);
  }

  &__setting {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--ke-space-4);
    cursor: pointer;
    border-radius: var(--ke-radius-sm);
    padding: var(--ke-space-2) var(--ke-space-2);
    margin: 0 calc(-1 * var(--ke-space-2));
    transition: background var(--ke-duration-fast);
    &:hover { background: var(--ke-gray-50); }
    &:focus-visible { outline: 2px solid var(--ke-brand-400); outline-offset: 2px; }
  }
  &__setting-text {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  &__setting-label { font-size: 14px; color: var(--ke-gray-800); }
  &__setting-desc { font-size: 12px; color: var(--ke-gray-400); }
  &__uploader :deep(.el-upload) {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--ke-space-2);
  }
  &__preview {
    background: var(--ke-brand-500);
    color: var(--ke-on-brand);
    font-weight: 600;
    font-size: 26px;
    cursor: pointer;
  }
  &__hint {
    font-size: 12px;
    color: var(--ke-gray-400);
  }
}

// 页面转场：淡入上移进 / 淡出下移留，方向感暗示内容替换
.ke-page-enter-active {
  transition: opacity 0.26s var(--ke-ease-out), transform 0.26s var(--ke-ease-out);
}
.ke-page-leave-active {
  transition: opacity 0.16s ease;
}
.ke-page-enter-from {
  opacity: 0;
  transform: translateY(12px);
}
.ke-page-leave-to {
  opacity: 0;
}
</style>
