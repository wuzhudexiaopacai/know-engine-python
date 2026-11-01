<script setup lang="ts">
// 应用外壳：顶部导航 + 角色感知的菜单 + 用户菜单；承载需登录的业务页。
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type UploadRequestOptions } from 'element-plus'
import { fetchProfile, updateProfile, uploadAvatar } from '@/api/auth'
import { getChatPreferences, updateChatPreferences } from '@/api/chat'
import { useAuthStore } from '@/stores/auth'
import { useTheme } from '@/composables/useTheme'
import type { ProfileUpdatePayload } from '@/types/auth'

const auth = useAuthStore()
const { isDark, toggle: toggleTheme } = useTheme()
const route = useRoute()
const router = useRouter()

interface NavItem {
  name: string
  label: string
  icon: string
  adminOnly?: boolean
}

const NAV: NavItem[] = [
  { name: 'chat', label: '智能问答', icon: 'ChatDotRound' },
  { name: 'documents', label: '知识文档', icon: 'Document', adminOnly: true },
  { name: 'qa-cache', label: '问答审核', icon: 'Stamp', adminOnly: true },
  { name: 'models', label: '模型配置', icon: 'Cpu', adminOnly: true },
  { name: 'users', label: '用户管理', icon: 'User', adminOnly: true },
]

const visibleNav = computed(() => NAV.filter((n) => !n.adminOnly || auth.isAdmin))
const activeName = computed(() => String(route.name ?? 'chat'))
const currentLabel = computed(
  () => NAV.find((n) => n.name === activeName.value)?.label ?? '工作台',
)

function go(name: string): void {
  if (route.name !== name) router.push({ name })
}

// 导航下划线指示器：跟随当前 Tab 平滑移动
const navEl = ref<HTMLElement | null>(null)
const indicator = reactive({ left: 0, width: 0, visible: false })

function syncIndicator(): void {
  const root = navEl.value
  if (!root) return
  const item = root.querySelector<HTMLElement>(`[data-nav="${activeName.value}"]`)
  if (!item) return
  indicator.left = item.offsetLeft
  indicator.width = item.offsetWidth
  indicator.visible = true
}

watch(activeName, () => nextTick(syncIndicator))
onMounted(() => {
  nextTick(syncIndicator)
  window.addEventListener('resize', syncIndicator)
})
onBeforeUnmount(() => window.removeEventListener('resize', syncIndicator))

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
  <div class="shell">
    <header class="shell__bar" role="banner">
      <div class="shell__brand" @click="go('chat')" tabindex="0"
             @keydown.enter="go('chat')" aria-label="返回智能问答">
        <span class="shell__logo" aria-hidden="true">K</span>
        <span class="shell__name">KnowEngine</span>
        <span class="shell__tag">企业级智能知识引擎</span>
      </div>

      <nav ref="navEl" class="shell__nav" aria-label="主导航">
        <button
          v-for="item in visibleNav"
          :key="item.name"
          class="shell__navitem"
          :class="{ 'is-active': activeName === item.name }"
          :data-nav="item.name"
          :aria-current="activeName === item.name ? 'page' : undefined"
          @click="go(item.name)"
        >
          <el-icon :size="16"><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </button>
        <span
          class="shell__nav-indicator"
          aria-hidden="true"
          :style="{
            transform: `translateX(${indicator.left}px)`,
            width: `${indicator.width}px`,
            opacity: indicator.visible ? 1 : 0,
          }"
        />
      </nav>

      <div class="shell__spacer" />

      <el-dropdown trigger="click" placement="bottom-end">
        <button class="shell__user" aria-label="用户菜单">
          <el-avatar :size="30" :src="auth.avatar || undefined" class="shell__avatar">{{ initial }}</el-avatar>
          <span class="shell__username ke-truncate">{{ auth.displayName || '未登录' }}</span>
          <el-icon :size="14"><ArrowDown /></el-icon>
        </button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item disabled>
              {{ auth.isAdmin ? '管理员' : '普通用户' }}
            </el-dropdown-item>
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
  display: flex;
  flex-direction: column;
  background: var(--ke-bg-page);

  &__bar {
    position: relative;
    height: var(--ke-header-h);
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: var(--ke-space-6);
    padding: 0 var(--ke-space-6);
    background: var(--ke-bg-card);
    border-bottom: 1px solid var(--ke-gray-100);
    box-shadow: var(--ke-shadow-sm);
    z-index: 10;
  }

  &__brand {
    display: flex;
    align-items: center;
    gap: var(--ke-space-2);
    cursor: pointer;
    user-select: none;
  }
  &__logo {
    width: 28px;
    height: 28px;
    border-radius: var(--ke-radius-sm);
    background: var(--ke-brand-500);
    color: var(--ke-on-brand);
    font-weight: 700;
    display: grid;
    place-items: center;
    font-size: 15px;
  }
  &__name {
    font-weight: 600;
    font-size: 15px;
    color: var(--ke-gray-900);
    letter-spacing: 0.2px;
  }
  &__tag {
    font-size: 12px;
    color: var(--ke-gray-400);
    padding-left: var(--ke-space-3);
    border-left: 1px solid var(--ke-gray-200);
    @media (max-width: 960px) {
      display: none;
    }
  }

  &__nav {
    position: relative;
    display: flex;
    align-items: center;
    gap: 2px;
    height: 100%;
  }
  &__navitem {
    position: relative;
    z-index: 1;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 100%;
    padding: 0 var(--ke-space-4);
    border: none;
    background: transparent;
    color: var(--ke-gray-700);
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    transition: color var(--ke-duration-base), transform var(--ke-duration-fast);

    // 非选中项仅文字加深，不使用灰色底块
    &:hover { background: transparent; color: var(--ke-gray-900); }
    &:active { transform: scale(0.97); }
    &.is-active {
      background: transparent;
      color: var(--ke-brand-600);
      font-weight: 600;
    }
  }

  // 选中下划线：一条独立指示器，随 Tab 切换平滑移动
  &__nav-indicator {
    position: absolute;
    left: 0;
    bottom: 0;
    height: 2px;
    border-radius: 2px;
    background: var(--ke-brand-500);
    pointer-events: none;
    transition: transform var(--ke-duration-base) var(--ke-ease-out),
                width var(--ke-duration-base) var(--ke-ease-out),
                opacity var(--ke-duration-fast);
  }

  &__spacer { flex: 1; }

  &__user {
    display: inline-flex;
    align-items: center;
    gap: var(--ke-space-2);
    padding: 4px 6px 4px 4px;
    border: 1px solid transparent;
    border-radius: var(--ke-radius-sm);
    background: transparent;
    cursor: pointer;
    transition: border-color var(--ke-duration-fast), background var(--ke-duration-fast);
    &:hover { border-color: var(--ke-gray-200); background: var(--ke-bg-hover); }
  }
  &__avatar {
    background: var(--ke-brand-500);
    color: var(--ke-on-brand);
    font-weight: 600;
  }
  &__username {
    max-width: 120px;
    font-size: 14px;
    color: var(--ke-gray-700);
  }
  &__motto {
    display: inline-block;
    max-width: 220px;
    font-size: 12px;
    font-style: italic;
    color: var(--ke-gray-400);
  }

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
