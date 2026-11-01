<script setup lang="ts">
// 智能问答主视图：编排会话列表、消息流、输入区与流式生成
import { nextTick, onBeforeUnmount, onMounted, ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Picture, Paperclip, ArrowDown } from '@element-plus/icons-vue'
import {
  listConversations, listMessages, deleteConversation, submitFeedback,
  generateImage, cancelImage, listChatModels, uploadChatImage,
  getChatSuggestions,
} from '@/api/chat'
import { BizError } from '@/api/http'
import type { Conversation, ChatMessageRecord, PublicModel } from '@/types/chat'
import type { UiMessage } from './types'
import { extractMdImages } from '@/utils/image'
import { useChatStream } from './composables/useChatStream'
import { useRecorder } from './composables/useRecorder'
import { useAuthStore } from '@/stores/auth'
import ConversationList from './components/ConversationList.vue'
import MessageBubble from './components/MessageBubble.vue'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const conversations = ref<Conversation[]>([])
const convLoading = ref(false)
const activeId = ref<string | null>(null)
const messages = ref<UiMessage[]>([])
const threadLoading = ref(false)
const input = ref('')
const useCache = ref(true)
/** 新会话空态建议问题（基于用户记忆动态拉取） */
const suggestions = ref<string[]>([
  'Redis 的持久化方案怎么选？',
  '微服务下的分布式事务怎么处理？',
  'Vue 项目构建太慢怎么优化？',
])
/** 用户可选模型（管理员在后台配置，此处只拿展示名） */
const models = ref<PublicModel[]>([])
const selectedModelId = ref<number | null>(null)
/** 管理员专属：是否查看全部用户的会话 */
const viewAll = ref(false)
const thread = ref<HTMLElement | null>(null)
/** 用户是否已经上滑离开底部：是 → 显示「回到最新消息」悬浮按钮 */
const showScrollBottom = ref(false)

/** 模型下拉：文字 + 小三角触发的悬浮列表 */
const modelMenuOpen = ref(false)
const modelRoot = ref<HTMLElement | null>(null)

function toggleModelMenu(): void {
  modelMenuOpen.value = !modelMenuOpen.value
}

function chooseModel(m: PublicModel): void {
  selectedModelId.value = m.id
  modelMenuOpen.value = false
}

/** 点击下拉外部区域时收起 */
function onDocClick(e: MouseEvent): void {
  if (!modelRoot.value?.contains(e.target as Node)) modelMenuOpen.value = false
}

const { streaming, send, stop, resume, cancel } = useChatStream()
const hasMessages = computed(() => messages.value.length > 0)

/** 待发送的图片：本地 objectURL 先出缩略图，上传完成后替换为服务端稳定地址 */
interface PendingImage {
  url: string
  name: string
  uploading: boolean
}
const pendingImages = ref<PendingImage[]>([])
const fileInput = ref<HTMLInputElement | null>(null)
const MAX_IMAGES = 4
const MAX_IMAGE_MB = 5
/** 有文字或有已上传图片才允许发送 */
const canSend = computed(() =>
  !!input.value.trim() || pendingImages.value.some((p) => !p.uploading))

/** 当前选中模型（用于在回答气泡上标注模型名） */
const selectedModel = computed(() =>
  models.value.find((m) => m.id === selectedModelId.value) ?? null)

/** 当前会话对象（判断归属与所有权） */
const activeConversation = computed(() =>
  conversations.value.find((c) => c.conversation_id === activeId.value) ?? null)

/** 管理员查看全部会话时，正在浏览他人会话则只读 */
const isForeign = computed(() => {
  const owner = activeConversation.value?.owner
  return !!(owner && auth.ownerKey && owner !== auth.ownerKey)
})

/** 语音输入：转写结果追加到输入框，不自动发送 */
const recorder = useRecorder((text) => {
  input.value = input.value ? `${input.value} ${text}` : text
})

/** 文生图弹窗状态 */
const IMG_SIZES = ['1024*1024', '1280*720', '720*1280']
const imageDialogVisible = ref(false)
const imagePrompt = ref('')
const imageSize = ref(IMG_SIZES[0])
const imageGenerating = ref(false)
/** 弹窗内联错误：失败/中断后就地提示，提示词保留可直接重发 */
const imageError = ref('')
const imageAbort = ref<AbortController | null>(null)
let imageRequestId = ''

/** 输入框占位随录音/转写状态切换，告诉用户此刻点在哪儿 */
const composerPlaceholder = computed(() => {
  if (isForeign.value) return '当前为他人会话，仅可查看'
  if (recorder.recording.value) return '正在聆听……再次点击麦克风结束并转写'
  if (recorder.transcribing.value) return '正在转写语音…'
  return '输入问题，Enter 发送，Shift + Enter 换行'
})

let seq = 0
function tempId(): string {
  return `local-${Date.now()}-${seq++}`
}

function scrollToBottom(smooth = false): void {
  nextTick(() => {
    const box = thread.value
    if (!box) return
    // 初次加载/切会话用瞬时贴底；流式生成期间保持贴底
    box.scrollTo({ top: box.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
    showScrollBottom.value = false
  })
  // 兜底：消息内图片/代码块等异步布局会在挂载后撑高容器，
  // 等一帧后再贴一次底，避免停在半截（尤其长会话首次打开）
  requestAnimationFrame(() => {
    const box = thread.value
    if (!box) return
    box.scrollTo({ top: box.scrollHeight, behavior: 'auto' })
    showScrollBottom.value = false
  })
}

/** 监听滚动：用户上滑超过阈值时显示「回到最新消息」按钮 */
function onThreadScroll(): void {
  const box = thread.value
  if (!box) return
  const distanceToBottom = box.scrollHeight - box.scrollTop - box.clientHeight
  showScrollBottom.value = distanceToBottom > 60
}

/** 后端历史消息 → 前端视图模型（用户侧图片 markdown 拆成附件展示） */
function toUiMessage(rec: ChatMessageRecord): UiMessage {
  const content = rec.content ?? ''
  const split = extractMdImages(content)
  return {
    id: rec.message_id,
    role: rec.type === 'USER' ? 'user' : 'assistant',
    content,
    text: rec.type === 'USER' ? split.text : undefined,
    images: rec.type === 'USER' ? split.images : undefined,
    streaming: false,
    references: rec.rag_references ?? [],
    card: null,
    transformContent: rec.transform_content,
    cacheHit: rec.cache_hit === 1,
    helpful: rec.helpful,
    modelName: rec.model_name,
    createdAt: new Date(rec.created_at).getTime(),
  }
}

async function loadConversations(): Promise<void> {
  convLoading.value = true
  try {
    conversations.value = await listConversations(viewAll.value && auth.isAdmin ? 'all' : 'self')
  } catch {
    /* 拦截器已提示 */
  } finally {
    convLoading.value = false
  }
}

/** 拉取可选模型；默认选中后端标记的默认项，否则取第一个 */
async function loadModels(): Promise<void> {
  try {
    models.value = await listChatModels()
    const preferred = models.value.find((m) => m.is_default === 1) ?? models.value[0]
    selectedModelId.value = preferred ? preferred.id : null
  } catch {
    /* 拦截器已提示 */
  }
}

/** 拉取新会话建议问题（基于用户记忆） */
async function loadSuggestions(): Promise<void> {
  try {
    const list = await getChatSuggestions()
    if (Array.isArray(list) && list.length) suggestions.value = list
  } catch {
    /* 拦截器已提示，保留兜底建议 */
  }
}

/** 管理员切换“全部会话”范围；切回自己时若正在看他人会话则清空 */
async function onToggleAll(value: boolean): Promise<void> {
  viewAll.value = value
  if (!value && isForeign.value) createConversation()
  await loadConversations()
}

/** 把当前会话写进地址栏（/chat/{id}），刷新后可直接回到同一会话 */
function syncUrl(id: string | null): void {
  const target = id ? `/chat/${id}` : '/chat'
  if (route.path !== target) void router.replace(target)
}

/**
 * 加载会话历史；若最后一条助手消息仍在服务端生成，则接管 SSE 续流。
 * 切换会话只断开前端连接（cancel），不触发后端中断。
 */
async function openConversation(id: string): Promise<void> {
  cancel()
  activeId.value = id
  threadLoading.value = true
  messages.value = []
  try {
    const records = await listMessages(id)
    messages.value = records.map(toUiMessage)
    scrollToBottom()
    const last = messages.value[messages.value.length - 1]
    if (last && last.role === 'assistant' && !isForeign.value) {
      // 不 await：续流可能持续很久，不能阻塞历史渲染
      void resume(id, last, {
        onDone: () => {
          void loadConversations()
          scrollToBottom()
        },
      })
    }
  } catch {
    activeId.value = null
    messages.value = []
    syncUrl(null)
  } finally {
    threadLoading.value = false
  }
}

async function selectConversation(id: string): Promise<void> {
  await openConversation(id)
  syncUrl(id)
}

function createConversation(): void {
  cancel()
  activeId.value = null
  messages.value = []
  input.value = ''
  pendingImages.value = []
  syncUrl(null)
  void loadSuggestions()
}

async function removeConversation(id: string): Promise<void> {
  try {
    await ElMessageBox.confirm('删除后该会话历史将不可恢复，确定删除吗？', '删除会话', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await deleteConversation(id)
    conversations.value = conversations.value.filter((c) => c.conversation_id !== id)
    if (activeId.value === id) createConversation()
    ElMessage.success('会话已删除')
  } catch {
    /* 拦截器已提示 */
  }
}

/** 组装助手占位消息并发起流式问答 */
async function ask(question: string, reuseTarget?: UiMessage,
                   images: string[] = []): Promise<void> {
  const text = question.trim()
  if ((!text && !images.length) || streaming.value || isForeign.value) return
  // 历史落库保留图片 markdown，刷新后仍能从 content 还原缩略图
  const displayContent = text + images.map((u) => `\n\n![](${u})`).join('')

  if (!reuseTarget) {
    messages.value.push({
      id: tempId(), role: 'user', content: displayContent,
      text: text || undefined, images: images.length ? images : undefined,
      streaming: false, references: [], createdAt: Date.now(),
    })
  }
  const target = reuseTarget ?? createAssistantPlaceholder()
  // 先标注本次选用的模型，流式期即可展示
  target.modelName = selectedModel.value?.display_name ?? null
  scrollToBottom()

  await send(
    {
      question: text, conversationId: activeId.value, useCache: useCache.value,
      modelId: selectedModelId.value, images,
    },
    target,
    {
      // 新会话 ID 一就绪即写入地址栏，无需等回答结束
      onConversation: (convId) => {
        if (!convId || activeId.value) return
        activeId.value = convId
        syncUrl(convId)
      },
      onSuggestions: (questions) => {
        target.suggestions = questions
      },
      onDone: (convId) => {
        if (convId) {
          activeId.value = convId
          syncUrl(convId)
        }
        void loadConversations()
      },
    },
  )
  scrollToBottom()
}

function createAssistantPlaceholder(): UiMessage {
  const placeholder: UiMessage = {
    id: tempId(), role: 'assistant', content: '', streaming: true,
    references: [], card: null, helpful: null, createdAt: Date.now(),
  }
  messages.value.push(placeholder)
  // 取回响应式代理引用，供 composable 逐 token 写入
  return messages.value[messages.value.length - 1]
}

function onSend(): void {
  modelMenuOpen.value = false
  if (streaming.value || isForeign.value) return
  const urls = pendingImages.value.filter((p) => !p.uploading).map((p) => p.url)
  const text = input.value.trim() || (urls.length ? '请分析这些图片' : '')
  if (!text) return
  input.value = ''
  pendingImages.value = []
  void ask(text, undefined, urls)
}

/** 重试：定位出错助手消息前一条用户提问，复用同一目标重新生成（含图片） */
function onRetry(target: UiMessage): void {
  const idx = messages.value.indexOf(target)
  let prev: UiMessage | null = null
  for (let i = idx - 1; i >= 0; i--) {
    if (messages.value[i].role === 'user') { prev = messages.value[i]; break }
  }
  if (!prev) return
  void ask(prev.text ?? prev.content, target, prev.images ?? [])
}

async function onFeedback(messageId: string, helpful: boolean): Promise<void> {
  try {
    await submitFeedback({ message_id: messageId, helpful })
    const m = messages.value.find((x) => x.id === messageId)
    if (m) m.helpful = helpful ? 1 : 0
    ElMessage.success('感谢反馈')
  } catch {
    /* 拦截器已提示 */
  }
}

function useSuggestion(s: string): void {
  input.value = s
}

/** 麦克风按钮：开始/停止录音；取消授权等失败只提示不打断页面 */
async function toggleRecord(): Promise<void> {
  if (!recorder.recording.value) {
    try {
      await recorder.start()
    } catch (e) {
      ElMessage.error(e instanceof Error ? e.message : '无法访问麦克风，请检查浏览器权限')
    }
    return
  }
  try {
    await recorder.stop()
  } catch {
    /* axios 拦截器已提示转写失败 */
  }
}

/** 选择图片：支持多选，最多 4 张、单张 5MB，上传成功后替换为服务端地址 */
function pickImages(): void {
  fileInput.value?.click()
}

async function onImagesPicked(e: Event): Promise<void> {
  const el = e.target as HTMLInputElement
  const files = Array.from(el.files ?? [])
  el.value = ''                       // 允许连续两次选同一文件
  if (!files.length) return
  const remain = MAX_IMAGES - pendingImages.value.length
  if (remain <= 0) {
    ElMessage.warning(`最多上传 ${MAX_IMAGES} 张图片`)
    return
  }
  for (const file of files.slice(0, remain)) {
    if (!file.type.startsWith('image/')) {
      ElMessage.warning(`${file.name} 不是图片文件`)
      continue
    }
    if (file.size > MAX_IMAGE_MB * 1024 * 1024) {
      ElMessage.warning(`${file.name} 超过 ${MAX_IMAGE_MB}MB`)
      continue
    }
    const item: PendingImage = {
      url: URL.createObjectURL(file), name: file.name, uploading: true,
    }
    pendingImages.value.push(item)
    try {
      const res = await uploadChatImage(file)
      item.url = res.url
    } catch {
      pendingImages.value = pendingImages.value.filter((x) => x !== item)
    } finally {
      item.uploading = false
    }
  }
}

function removePendingImage(index: number): void {
  const [removed] = pendingImages.value.splice(index, 1)
  if (removed?.url.startsWith('blob:')) URL.revokeObjectURL(removed.url)
}

function openImageDialog(): void {
  imageError.value = ''
  imageDialogVisible.value = true
}

/** 停止生成：断开请求 + 让后端停止轮询，提示词保留供修改后重发 */
function stopImageGeneration(): void {
  if (!imageGenerating.value) return
  imageAbort.value?.abort()
  if (imageRequestId) void cancelImage(imageRequestId).catch(() => { /* 已结束则忽略 */ })
}

/** 关闭弹窗视为放弃本次生成 */
function onImageDialogClosed(): void {
  if (imageGenerating.value) stopImageGeneration()
}

/** 文生图：后端同步等异步任务，成功后提问与图片消息插入会话（已落库） */
async function runImageGeneration(): Promise<void> {
  const prompt = imagePrompt.value.trim()
  if (!prompt || imageGenerating.value) return
  const controller = new AbortController()
  const requestId = `${Date.now()}-${seq++}`
  imageAbort.value = controller
  imageRequestId = requestId
  imageError.value = ''
  imageGenerating.value = true
  try {
    const res = await generateImage({
      prompt, conversation_id: activeId.value, size: imageSize.value,
      request_id: requestId,
    }, controller.signal)
    imageDialogVisible.value = false
    imagePrompt.value = ''
    const now = Date.now()
    const split = extractMdImages(res.user_message.content)
    messages.value.push(
      { id: res.user_message.message_id || tempId(), role: 'user', content: prompt,
        text: split.text, images: res.urls, streaming: false, references: [], createdAt: now },
      { id: res.assistant_message.message_id || tempId(), role: 'assistant',
        content: res.assistant_message.content, streaming: false, references: [],
        card: null, helpful: null, createdAt: now + 1 },
    )
    if (!activeId.value) {
      activeId.value = res.conversation_id
      syncUrl(res.conversation_id)
      void loadConversations()
    }
    scrollToBottom()
  } catch (e) {
    // 主动中断不弹全局提示；其余失败把原因留在弹窗里，方便直接重试
    imageError.value = controller.signal.aborted
      ? '已停止生成，可修改描述后重新生成'
      : (e instanceof BizError ? e.message : '生成失败，请检查网络后重新生成')
  } finally {
    imageGenerating.value = false
    imageAbort.value = null
    imageRequestId = ''
  }
}

onMounted(async () => {
  document.addEventListener('click', onDocClick)
  await Promise.all([loadConversations(), loadModels(), loadSuggestions()])
  const routeId = typeof route.params.conversationId === 'string' && route.params.conversationId
    ? route.params.conversationId : null
  if (routeId) {
    await openConversation(routeId)
    return
  }
  // 地址栏只有 /chat：自动选中最近活跃会话，并把会话 ID 写回地址栏
  const recent = conversations.value[0]
  if (recent) {
    await openConversation(recent.conversation_id)
    syncUrl(recent.conversation_id)
  }
})

// 前进/后退：跟随地址栏参数切换会话，避免只改 URL 不换内容
watch(() => route.params.conversationId, (value) => {
  const next = typeof value === 'string' && value ? value : null
  if (next === activeId.value) return
  if (next) {
    void openConversation(next)
    return
  }
  // 仅切到 /chat（如点导航“智能问答”）：保留当前会话并把 ID 补回地址栏
  if (activeId.value) syncUrl(activeId.value)
  else createConversation()
})

onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <div class="chat">
    <ConversationList
      :conversations="conversations"
      :active-id="activeId"
      :loading="convLoading"
      :can-view-all="auth.isAdmin"
      :view-all="viewAll"
      @select="selectConversation"
      @create="createConversation"
      @remove="removeConversation"
      @toggle-all="onToggleAll"
    />

    <section class="chat__main" :class="{ 'is-streaming': streaming }" aria-label="对话区" aria-live="polite">
      <div ref="thread" class="chat__thread" @scroll="onThreadScroll">
        <el-skeleton v-if="threadLoading" :rows="6" animated class="chat__skeleton" />

        <div v-else-if="!hasMessages" class="chat__welcome">
          <h2 class="chat__welcome-title">有什么可以帮你？</h2>
          <p class="chat__welcome-sub">基于企业知识库的智能问答，答案附引用溯源</p>
          <div class="chat__chips">
            <button v-for="s in suggestions" :key="s" class="chat__chip ke-enter-up" @click="useSuggestion(s)">
              {{ s }}
            </button>
          </div>
        </div>

        <MessageBubble
          v-else
          v-for="m in messages"
          :key="m.id"
          :message="m"
          @feedback="onFeedback"
          @retry="onRetry"
          @stop="stop"
          @suggest="useSuggestion"
        />

        <!-- 回到最新消息：上滑看历史时出现，点击瞬时滚到底部 -->
        <transition name="chat-fab">
          <el-button
            v-if="showScrollBottom && hasMessages && !threadLoading"
            class="chat__scroll-bottom"
            circle
            :icon="ArrowDown"
            aria-label="回到最新消息"
            @click="scrollToBottom()"
          />
        </transition>
      </div>

      <footer class="chat__composer">
        <div class="chat__composer-inner ke-card">
          <input
            ref="fileInput"
            type="file"
            accept="image/*"
            multiple
            class="chat__file-input"
            @change="onImagesPicked"
          />
          <!-- 待发送图片：上传中显示遮罩，可逐张移除 -->
          <div v-if="pendingImages.length" class="chat__attachments">
            <div v-for="(img, i) in pendingImages" :key="i" class="chat__attachment">
              <img :src="img.url" :alt="img.name" />
              <span v-if="img.uploading" class="chat__attachment-mask">
                <el-icon class="is-loading"><Loading /></el-icon>
              </span>
              <button
                type="button"
                class="chat__attachment-remove"
                aria-label="移除图片"
                @click="removePendingImage(i)"
              >
                <el-icon><Close /></el-icon>
              </button>
            </div>
          </div>
          <el-input
            v-model="input"
            type="textarea"
            :autosize="{ minRows: 1, maxRows: 5 }"
            resize="none"
            :placeholder="composerPlaceholder"
            :disabled="recorder.transcribing.value || isForeign"
            @keydown.enter.exact.prevent="onSend"
          />
          <div class="chat__composer-bar">
            <div class="chat__bar-left">
              <label class="chat__cache">
                <el-switch v-model="useCache" size="small" :disabled="isForeign" />
                <span>优先命中问答缓存</span>
              </label>
              <div
                v-if="models.length"
                ref="modelRoot"
                class="chat__model"
                :class="{ 'is-open': modelMenuOpen }"
              >
                <span class="chat__model-label">模型</span>
                <button
                  type="button"
                  class="chat__model-trigger"
                  :disabled="streaming || isForeign"
                  aria-haspopup="listbox"
                  :aria-expanded="modelMenuOpen"
                  @click="toggleModelMenu"
                >
                  <span class="chat__model-name">{{ selectedModel?.display_name ?? '选择模型' }}</span>
                  <el-icon class="chat__model-arrow" :size="12"><ArrowDown /></el-icon>
                </button>
                <transition name="model-pop">
                  <ul v-if="modelMenuOpen" class="chat__model-menu" role="listbox" aria-label="模型列表">
                    <li
                      v-for="m in models"
                      :key="m.id"
                      class="chat__model-option"
                      :class="{ 'is-selected': m.id === selectedModelId }"
                      role="option"
                      :aria-selected="m.id === selectedModelId"
                      @click="chooseModel(m)"
                    >
                      <span class="chat__model-option-name">{{ m.display_name }}</span>
                      <el-icon v-if="m.id === selectedModelId" :size="12"><Select /></el-icon>
                    </li>
                  </ul>
                </transition>
              </div>
            </div>
            <div class="chat__actions">
              <el-tooltip content="添加图片（最多 4 张）" placement="top">
                <el-button
                  :icon="Paperclip"
                  circle
                  :disabled="streaming || isForeign || pendingImages.length >= MAX_IMAGES"
                  aria-label="添加图片"
                  @click="pickImages"
                />
              </el-tooltip>
              <el-tooltip content="生成图片" placement="top">
                <el-button :icon="Picture" circle :disabled="streaming || isForeign" aria-label="生成图片" @click="openImageDialog" />
              </el-tooltip>
              <el-tooltip
                :content="recorder.supported ? (recorder.recording.value ? '停止录音并转写' : '语音输入') : '当前环境不支持录音'"
                placement="top"
              >
                <button
                  type="button"
                  class="chat__mic"
                  :class="{ 'is-recording': recorder.recording.value }"
                  :disabled="!recorder.supported || recorder.transcribing.value || streaming || isForeign"
                  aria-label="语音输入"
                  @click="toggleRecord"
                >
                  <el-icon v-if="recorder.transcribing.value" class="is-loading"><Loading /></el-icon>
                  <el-icon v-else><Microphone /></el-icon>
                </button>
              </el-tooltip>
              <el-button v-if="streaming" @click="stop">
                <el-icon><Close /></el-icon>&nbsp;停止
              </el-button>
              <el-button type="primary" :loading="streaming" :disabled="!canSend || isForeign" @click="onSend">
                <el-icon><Promotion /></el-icon>&nbsp;发送
              </el-button>
            </div>
          </div>
        </div>
        <p class="chat__disclaimer ke-muted">内容亦可能有不准确之处，请以权威资料为准</p>
      </footer>
    </section>

    <!-- 文生图弹窗：提交后后端同步等待异步任务，按钮保持加载态 -->
    <el-dialog
      v-model="imageDialogVisible"
      title="生成图片"
      width="min(520px, 92vw)"
      append-to-body
      @closed="onImageDialogClosed"
    >
      <el-input
        v-model="imagePrompt"
        type="textarea"
        :autosize="{ minRows: 2, maxRows: 4 }"
        maxlength="800"
        show-word-limit
        resize="none"
        placeholder="描述你想生成的画面，例如：分布式事务 TCC 模式的三步流程示意图，简洁扁平风"
      />
      <div class="chat__image-options">
        <span class="chat__image-label">画面尺寸</span>
        <el-radio-group v-model="imageSize" size="small" :disabled="imageGenerating">
          <el-radio-button v-for="s in IMG_SIZES" :key="s" :value="s">{{ s }}</el-radio-button>
        </el-radio-group>
      </div>
      <p v-if="imageError" class="chat__image-error">{{ imageError }}</p>
      <template #footer>
        <el-button v-if="imageGenerating" @click="stopImageGeneration">停止生成</el-button>
        <el-button v-else @click="imageDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="imageGenerating"
          :disabled="!imagePrompt.trim()"
          @click="runImageGeneration"
        >
          {{ imageGenerating ? '绘制中，约需半分钟…' : (imageError ? '重新生成' : '开始生成') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.chat {
  display: flex; height: 100%; min-height: 0; background: var(--ke-bg-page);

  &__main {
    position: relative;
    flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0;
    background: var(--ke-bg-card);
  }

  // overflow-x: hidden：兜底裁掉横向溢出，横向滚动条只出现在代码块内部
  &__thread {
    position: relative; z-index: 1; flex: 1; min-height: 0;
    overflow-y: auto; overflow-x: hidden;
    padding: var(--ke-space-6) var(--ke-space-6) var(--ke-space-2);
  }
  &__skeleton { max-width: 760px; margin: 0 auto; }

  &__welcome { text-align: center; padding: 56px var(--ke-space-4) var(--ke-space-6); }
  &__welcome-title { margin: 0 0 6px; font-size: 20px; font-weight: 600; color: var(--ke-gray-900); }
  &__welcome-sub { margin: 0 0 var(--ke-space-5); font-size: 13px; color: var(--ke-gray-500); }
  &__chips { display: flex; flex-wrap: wrap; gap: var(--ke-space-2); justify-content: center; }
  &__chip {
    border: 1px solid var(--ke-brand-100); background: var(--ke-bg-card); color: var(--ke-gray-700);
    border-radius: 999px; padding: 6px 14px; font-size: 13px; cursor: pointer;
    transition: border-color var(--ke-duration-fast), color var(--ke-duration-fast),
                background var(--ke-duration-fast);
    &:hover {
      border-color: var(--ke-brand-400);
      background: var(--ke-brand-50);
      color: var(--ke-brand-600);
    }
  }

  // 消息、输入区居中于收窄的阅读宽度，避免大片留白
  &__thread > :not(.chat__welcome) { max-width: 780px; margin: 0 auto; }

  // 回到最新消息：悬浮在消息区右下角（输入框上方），上滑看历史时出现
  &__scroll-bottom {
    position: absolute;
    right: 28px;
    bottom: 24px;
    z-index: 4;
    box-shadow: 0 6px 20px rgba(17, 24, 39, 0.18);
  }

  &__composer { position: relative; z-index: 1; flex: 0 0 auto; padding: var(--ke-space-3) var(--ke-space-6) var(--ke-space-4); }
  &__composer-inner {
    position: relative;
    max-width: 780px; margin: 0 auto; padding: var(--ke-space-2) var(--ke-space-3);
    border-radius: var(--ke-radius-md);
    background: var(--ke-bg-card);
    transition: border-color var(--ke-duration-fast), background-color var(--ke-duration-fast),
                box-shadow var(--ke-duration-fast);
    :deep(.el-textarea__inner) {
      box-shadow: none; padding: 4px 6px; font-size: 14px; background: transparent;
    }

    // 聚焦：蓝色描边 + 背景轻微提亮
    &:focus-within {
      border-color: var(--ke-brand-400);
      background: var(--ke-bg-elevated);
      box-shadow: 0 0 0 3px var(--ke-ring);
    }
  }
  // 隐藏原生文件选择器，由回形针按钮触发
  &__file-input { display: none; }
  &__attachments {
    display: flex; flex-wrap: wrap; gap: var(--ke-space-2);
    padding: var(--ke-space-1) var(--ke-space-1) var(--ke-space-2);
  }
  &__attachment {
    position: relative; width: 64px; height: 64px; border-radius: var(--ke-radius-sm);
    overflow: hidden; border: 1px solid var(--ke-gray-200); background: var(--ke-gray-50);
    img { width: 100%; height: 100%; object-fit: cover; display: block; }
  }
  &__attachment-mask {
    position: absolute; inset: 0; display: grid; place-items: center;
    background: rgba(255, 255, 255, 0.72); color: var(--ke-brand-600); font-size: 16px;
  }
  &__attachment-remove {
    position: absolute; top: 2px; right: 2px; width: 18px; height: 18px;
    display: grid; place-items: center; padding: 0;
    border: none; border-radius: 50%; cursor: pointer;
    background: rgba(17, 24, 39, 0.62); color: var(--ke-on-brand); font-size: 12px;
    transition: background var(--ke-duration-fast), transform var(--ke-duration-fast);
    &:hover { background: rgba(17, 24, 39, 0.85); transform: scale(1.06); }
  }
  &__composer-bar {
    display: flex; align-items: center; justify-content: space-between;
    margin-top: var(--ke-space-2);
  }
  &__cache { display: inline-flex; align-items: center; gap: var(--ke-space-2); font-size: 13px; color: var(--ke-gray-500); cursor: pointer; }
  &__bar-left { display: flex; align-items: center; gap: var(--ke-space-4); min-width: 0; }
  &__model { position: relative; display: inline-flex; align-items: center; gap: 6px; }
  &__model-label { font-size: 13px; color: var(--ke-gray-500); white-space: nowrap; }
  // 模型选择：文字 + 小三角，无独立输入框
  &__model-trigger {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 2px 4px; margin: -2px -4px;
    border: none; background: transparent; border-radius: var(--ke-radius-sm);
    font-size: 13px; color: var(--ke-gray-700); cursor: pointer;
    transition: color var(--ke-duration-fast), background var(--ke-duration-fast),
                transform var(--ke-duration-fast);
    &:hover:not(:disabled) { color: var(--ke-brand-600); background: var(--ke-brand-50); }
    &:active:not(:disabled) { transform: scale(0.97); }
    &:disabled { cursor: not-allowed; opacity: 0.6; }
  }
  &__model-arrow {
    color: var(--ke-gray-400);
    transition: transform var(--ke-duration-base) var(--ke-ease-out), color var(--ke-duration-fast);
  }
  &__model.is-open &__model-arrow { transform: rotate(180deg); color: var(--ke-brand-500); }

  // 悬浮下拉：向上展开，柔和缩放淡入
  &__model-menu {
    position: absolute; left: -6px; bottom: calc(100% + 8px); z-index: 30;
    transform-origin: bottom left;
    min-width: 200px; max-height: 240px; overflow-y: auto;
    margin: 0; padding: 4px; list-style: none;
    background: var(--ke-bg-elevated); border: 1px solid var(--ke-brand-100);
    border-radius: var(--ke-radius-md); box-shadow: var(--ke-shadow-md);
  }
  &__model-option {
    display: flex; align-items: center; justify-content: space-between; gap: var(--ke-space-3);
    padding: 8px 10px; border-radius: var(--ke-radius-sm);
    font-size: 13px; color: var(--ke-gray-700); cursor: pointer;
    transition: background var(--ke-duration-fast), color var(--ke-duration-fast),
                transform var(--ke-duration-fast);
    &:hover {
      background: var(--ke-brand-50);
      color: var(--ke-brand-700);
      transform: scale(1.01);
    }
    &.is-selected { background: var(--ke-brand-50); color: var(--ke-brand-700); font-weight: 600; }
  }
  &__actions { display: flex; align-items: center; gap: var(--ke-space-2); }

  // 语音输入钮：与 el-button 圆形钮同尺寸；录音态红底 + 柔和脉冲（仅动效反馈）
  &__mic {
    width: 32px; height: 32px; border-radius: 50%; cursor: pointer;
    display: inline-grid; place-items: center;
    border: 1px solid var(--ke-gray-200); background: var(--ke-bg-card); color: var(--ke-gray-600);
    transition: border-color var(--ke-duration-base), color var(--ke-duration-base),
                background var(--ke-duration-base);
    &:hover:not(:disabled) { border-color: var(--ke-brand-400); color: var(--ke-brand-600); }
    &:disabled { cursor: not-allowed; opacity: 0.5; }
    &.is-recording {
      background: var(--ke-danger); border-color: var(--ke-danger); color: var(--ke-on-brand);
      box-shadow: 0 0 0 3px var(--ke-ring-danger);
    }
  }
  &__disclaimer { text-align: center; font-size: 12px; margin: var(--ke-space-2) 0 0; }
}

.chat__image-options {
  display: flex; align-items: center; gap: var(--ke-space-3);
  margin-top: var(--ke-space-4); flex-wrap: wrap;
}
.chat__image-label { font-size: 13px; color: var(--ke-gray-500); }
.chat__image-error {
  margin: var(--ke-space-3) 0 0; font-size: 13px; color: var(--ke-danger);
  background: var(--ke-danger-50); border-radius: var(--ke-radius-sm, 6px);
  padding: 6px 10px;
}

// 模型下拉展开/收起：轻微缩放 + 淡入
.model-pop-enter-active,
.model-pop-leave-active {
  transition: opacity 0.18s var(--ke-ease-out), transform 0.18s var(--ke-ease-out);
}
.model-pop-enter-from,
.model-pop-leave-to { opacity: 0; transform: translateY(6px) scale(0.98); }

// 回到最新消息按钮：上滑时淡入，贴底时淡出
.chat-fab-enter-active,
.chat-fab-leave-active { transition: opacity 0.2s var(--ke-ease-out), transform 0.2s var(--ke-ease-out); }
.chat-fab-enter-from,
.chat-fab-leave-to { opacity: 0; transform: translateY(8px); }

// 图标按钮：hover 提亮并轻微放大
.chat__actions .el-button .el-icon,
.chat__mic .el-icon { transition: transform var(--ke-duration-fast), color var(--ke-duration-fast); }
.chat__actions .el-button:hover:not(.is-disabled) .el-icon,
.chat__mic:hover:not(:disabled) .el-icon { transform: scale(1.08); }

// 流式生成中：输入卡片仅加强描边，表达“处理中”而不干扰阅读
.chat__main.is-streaming .chat__composer-inner {
  border-color: var(--ke-brand-400);
  box-shadow: 0 0 0 3px var(--ke-ring);
}

@media (max-width: 720px) {
  .chat { position: relative; }
  .chat__conv { display: none; }
}
</style>
