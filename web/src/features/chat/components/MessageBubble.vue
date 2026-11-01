<script setup lang="ts">
// 单条消息气泡：区分用户 / 助手，助手侧承载进度、引用溯源、卡片、评价
import { computed, ref } from 'vue'
import type { UiMessage } from '../types'
import type { RagReference, ReferenceDetail } from '@/types/chat'
import type { RetrievalSource } from '@/types/common'
import { getReferenceDetail } from '@/api/chat'
import MarkdownView from '@/components/common/MarkdownView.vue'
import ResultCardView from './ResultCardView.vue'

const props = defineProps<{ message: UiMessage }>()

const emit = defineEmits<{
  (e: 'feedback', messageId: string, helpful: boolean): void
  (e: 'stop'): void
  (e: 'retry', message: UiMessage): void
  (e: 'suggest', question: string): void
}>()

/** 检索来源 → 展示标签 */
const SOURCE_LABEL: Record<RetrievalSource, string> = {
  vector: '向量',
  fulltext: '全文',
  hybrid: '混合',
  sql: 'SQL',
  graph: '图谱',
  qa_cache: '缓存',
}

const isUser = computed(() => props.message.role === 'user')
/** 助手消息正文是否可展示（非纯流式空态） */
const hasContent = computed(() => props.message.content.length > 0)
/** 仅在答案完成且可评价时展示评价条 */
const canFeedback = computed(
  () => !isUser.value && !props.message.streaming && !!props.message.id && hasContent.value,
)

const refsOpen = ref(false)
function scoreText(v: number | null): string {
  return v === null ? '—' : v.toFixed(3)
}

/** 引用预览：按 chunkId 回查文档章节与段序；缓存来源没有 chunkId 时展示快照片段 */
const previewOpen = ref(false)
const previewLoading = ref(false)
const previewError = ref('')
const previewDetail = ref<ReferenceDetail | null>(null)
const activeRef = ref<RagReference | null>(null)

async function openReference(r: RagReference): Promise<void> {
  activeRef.value = r
  previewDetail.value = null
  previewError.value = ''
  previewOpen.value = true
  if (!r.chunkId) {
    previewError.value = '该引用来自缓存答案的来源快照，暂无精确片段定位'
    return
  }
  previewLoading.value = true
  try {
    previewDetail.value = await getReferenceDetail(r.chunkId)
  } catch {
    previewError.value = '引用已失效或无法定位，以下展示缓存的片段摘要'
  } finally {
    previewLoading.value = false
  }
}
</script>

<template>
  <article class="msg ke-enter-up" :class="isUser ? 'msg--user' : 'msg--assistant'">
    <div v-if="!isUser" class="msg__avatar" :class="{ 'is-thinking': message.streaming }" aria-hidden="true">K</div>

    <div class="msg__body">
      <!-- 元信息：缓存命中与所用模型 -->
      <div v-if="!isUser && (message.cacheHit || message.modelName)" class="msg__meta">
        <el-tag v-if="message.cacheHit" size="small" type="success" effect="plain">
          <el-icon><Lightning /></el-icon>&nbsp;命中问答缓存
        </el-tag>
        <el-tag v-if="message.modelName" size="small" effect="plain" type="info">
          模型 · {{ message.modelName }}
        </el-tag>
      </div>

      <!-- 改写后检索问题（可选，弱提示） -->
      <p v-if="!isUser && message.transformContent" class="msg__transform">
        检索意图：{{ message.transformContent }}
      </p>

      <!-- 进度占位：流式期尚未产出正文时展示点阵加载与阶段文案 -->
      <div v-if="!isUser && message.streaming && !hasContent" class="msg__progress">
        <span class="msg__dots" aria-hidden="true"><i /><i /><i /></span>
        <span>{{ message.progress || '正在思考…' }}</span>
        <button class="msg__stop" @click="emit('stop')" aria-label="停止生成">
          <el-icon><Close /></el-icon> 停止
        </button>
      </div>

      <!-- 正文：助手侧 Markdown+公式渲染（DOMPurify 消毒），用户侧纯文本防注入 -->
      <div
        v-else
        class="msg__content"
        :class="{
          'msg__content--user': isUser,
          'msg__content--images-only': isUser && !!message.images?.length && !message.text,
        }"
      >
        <template v-if="isUser">
          <p v-if="message.text || !message.images?.length">{{ message.text ?? message.content }}</p>
        </template>
        <template v-else>
          <MarkdownView :text="message.content" :streaming="message.streaming" />
          <span v-if="message.streaming" class="msg__caret" aria-hidden="true"></span>
        </template>
      </div>

      <!-- 用户侧生成的图片附件（文生图提问气泡内缩略展示） -->
      <div v-if="isUser && message.images?.length" class="msg__images">
        <img v-for="(src, i) in message.images" :key="i" :src="src" alt="生成的图片" loading="lazy" />
      </div>

      <!-- 结构化卡片 -->
      <ResultCardView v-if="!isUser && message.card" :card="message.card" />

      <!-- 引用溯源 -->
      <div v-if="!isUser && message.references.length" class="msg__refs">
        <button class="msg__refs-toggle" @click="refsOpen = !refsOpen"
                :aria-expanded="refsOpen" aria-label="展开或收起引用来源">
          <el-icon><DocumentCopy /></el-icon>
          引用 {{ message.references.length }} 条
          <el-icon class="msg__refs-chevron" :class="{ 'is-open': refsOpen }"><ArrowDown /></el-icon>
        </button>
        <ul v-show="refsOpen" class="msg__refs-list">
          <li v-for="(r, i) in message.references" :key="i" class="msg__ref"
              role="button" tabindex="0"
              :aria-label="`查看引用片段：${r.documentTitle || '未知文档'}`"
              @click="openReference(r)"
              @keydown.enter.prevent="openReference(r)">
            <div class="msg__ref-head">
              <el-tag size="small" effect="plain">{{ SOURCE_LABEL[r.retrievalSource] ?? r.retrievalSource }}</el-tag>
              <span class="msg__ref-title ke-truncate">{{ r.documentTitle || '未知文档' }}</span>
              <span class="msg__ref-score">相似度 {{ scoreText(r.similarityScore) }}</span>
            </div>
            <p class="msg__ref-snippet">{{ r.chunkContent }}</p>
            <span class="msg__ref-more">查看片段</span>
          </li>
        </ul>
      </div>

      <!-- 错误与重试 -->
      <div v-if="!isUser && message.error" class="msg__error">
        <el-icon><WarningFilled /></el-icon>
        <span>{{ message.error }}</span>
        <button class="msg__retry" @click="emit('retry', message)" aria-label="重试本条提问">重试</button>
      </div>

      <!-- 评价 -->
      <div v-if="canFeedback" class="msg__feedback" role="group" aria-label="答案质量反馈">
        <template v-if="message.helpful === null || message.helpful === undefined">
          <button class="msg__fb-btn" @click="emit('feedback', message.id, true)" aria-label="有帮助">
            <el-icon><Select /></el-icon> 有帮助
          </button>
          <button class="msg__fb-btn" @click="emit('feedback', message.id, false)" aria-label="没帮助">
            <el-icon><Close /></el-icon> 没帮助
          </button>
        </template>
        <span v-else class="msg__fb-done ke-muted">
          {{ message.helpful === 1 ? '已标记为有帮助' : '已标记为待改进' }}
        </span>
      </div>

      <!-- follow-up 建议问题：回答结束后展示，点击填入输入框 -->
      <div v-if="!isUser && message.suggestions?.length && !message.streaming" class="msg__suggests">
        <span class="msg__suggests-label">你可能还想问：</span>
        <button
          v-for="q in message.suggestions"
          :key="q"
          type="button"
          class="msg__suggest-chip"
          @click="emit('suggest', q)"
        >
          {{ q }}
        </button>
      </div>
    </div>
  </article>

  <!-- 引用片段预览：定位到文档章节与段序 -->
  <el-dialog v-model="previewOpen" width="min(720px, 92vw)" append-to-body
             class="ref-dialog" :show-close="true">
    <template #header>
      <div class="ref-dialog__head">
        <span class="ref-dialog__title">
          {{ previewDetail?.documentTitle || activeRef?.documentTitle || '引用来源' }}
        </span>
        <span v-if="previewDetail" class="ref-dialog__meta">
          <span v-if="previewDetail.version">版本 {{ previewDetail.version }}</span>
          <span v-if="previewDetail.sectionPath">· {{ previewDetail.sectionPath }}</span>
        </span>
      </div>
    </template>

    <div v-loading="previewLoading" class="ref-dialog__body">
      <div v-if="previewDetail" class="ref-dialog__crumb">
        <el-tag v-if="activeRef" size="small" effect="plain">
          {{ SOURCE_LABEL[activeRef.retrievalSource] ?? activeRef.retrievalSource }}
        </el-tag>
        <span v-if="previewDetail.sectionPath">章节：{{ previewDetail.sectionPath }}</span>
        <span>位置：第 {{ previewDetail.order }} / 共 {{ previewDetail.total }} 段</span>
        <span v-if="previewDetail.truncated" class="ke-muted">（仅展示前 4000 字）</span>
      </div>
      <p v-else-if="previewError" class="ref-dialog__tip">{{ previewError }}</p>
      <div class="ref-dialog__content">
        <MarkdownView
          v-if="previewDetail?.content || activeRef?.chunkContent"
          :text="previewDetail?.content || activeRef?.chunkContent || ''"
        />
        <p v-else class="ref-dialog__empty">暂无片段内容</p>
      </div>
    </div>

    <template #footer>
      <el-button @click="previewOpen = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped lang="scss">
.msg {
  display: flex;
  gap: var(--ke-space-3);
  margin-bottom: var(--ke-space-6);

  &--user { justify-content: flex-end; }

  &__avatar {
    flex: 0 0 auto;
    width: 32px; height: 32px; border-radius: var(--ke-radius-sm);
    background: var(--ke-brand-500);
    color: var(--ke-on-brand);
    display: grid; place-items: center; font-weight: 600; font-size: 14px;
  }

  // min-width: 0 关键：flex 子项默认按内容撑开，长代码行会把整条消息撑宽、溢出对话区
  &__body { display: flex; flex-direction: column; gap: var(--ke-space-2); min-width: 0; }
  &--assistant &__body { align-items: flex-start; flex: 1; min-width: 0; }
  &--user &__body { align-items: flex-end; max-width: 78%; min-width: 0; }

  &__meta { display: flex; align-items: center; gap: var(--ke-space-2); align-self: flex-start; }
  &__transform { margin: 0; font-size: 12px; color: var(--ke-gray-400); }

  // 纯图片的用户消息：去掉气泡底板让图片自带圆角展示
  &__content--images-only {
    padding: 0; background: transparent; border: none;
  }
  &__images {
    display: flex; flex-wrap: wrap; gap: var(--ke-space-2); margin-top: var(--ke-space-2);
    img {
      max-width: 320px; max-height: 320px; border-radius: var(--ke-radius-md);
      border: 1px solid var(--ke-gray-200); object-fit: cover;
    }
  }

  &__content {
    word-break: break-word;
    min-width: 0;
    max-width: 100%;
    color: var(--ke-gray-800);

    // AI 回答：白底正文，作为标准内容区而非悬浮气泡
    &:not(&--user) {
      white-space: normal;
      padding: 2px 0 0;
      font-size: 15px;
      line-height: 1.85;
    }

    // 用户提问：柔和浅蓝底，上下内边距收窄，更紧凑
    &--user {
      white-space: pre-wrap;
      padding: 8px 12px;
      border-radius: var(--ke-radius-md);
      background: var(--ke-brand-50);
      border: 1px solid var(--ke-brand-100);
      color: var(--ke-gray-800);
      font-size: 14px;
      line-height: 1.7;
      p { margin: 0; }
    }
  }

  &__progress {
    display: flex; align-items: center; gap: var(--ke-space-2);
    padding: 6px 0;
    font-size: 13px; color: var(--ke-gray-500);
  }
  &__dots {
    display: inline-flex; align-items: center; gap: 4px;
    i {
      width: 5px; height: 5px; border-radius: 50%;
      background: var(--ke-brand-400);
      animation: ke-dot 1.2s ease-in-out infinite;
      &:nth-child(2) { animation-delay: 0.16s; }
      &:nth-child(3) { animation-delay: 0.32s; }
    }
  }
  &__stop {
    margin-left: var(--ke-space-2); display: inline-flex; align-items: center; gap: 2px;
    border: 1px solid var(--ke-gray-200); background: var(--ke-bg-card); color: var(--ke-gray-600);
    border-radius: var(--ke-radius-sm); padding: 2px 8px; font-size: 12px; cursor: pointer;
    transition: border-color var(--ke-duration-fast), color var(--ke-duration-fast);
    &:hover { border-color: var(--ke-danger); color: var(--ke-danger); }
  }

  &__caret {
    display: inline-block; width: 2px; height: 15px; margin-left: 3px;
    border-radius: 1px;
    background: var(--ke-brand-500);
    vertical-align: text-bottom;
    animation: ke-blink 1s steps(2) infinite;
  }

  &__refs { align-self: stretch; width: 100%; max-width: 460px; }
  &__refs-toggle {
    display: inline-flex; align-items: center; gap: 6px;
    border: none; background: transparent; cursor: pointer;
    color: var(--ke-brand-600); font-size: 13px; padding: 4px 0;
  }
  &__refs-chevron { transition: transform 0.18s; &.is-open { transform: rotate(180deg); } }
  &__refs-list {
    list-style: none; margin: var(--ke-space-1) 0 0; padding: 0;
    display: flex; flex-direction: column; gap: var(--ke-space-2);
  }
  &__ref {
    border: 1px solid var(--ke-gray-200); border-radius: var(--ke-radius-sm);
    padding: var(--ke-space-2) var(--ke-space-3); background: var(--ke-gray-50);
    cursor: pointer;
    transition: border-color var(--ke-duration-fast), background var(--ke-duration-fast);
    &:hover { border-color: var(--ke-brand-300); background: var(--ke-brand-50); }
    &:focus-visible { outline: 2px solid var(--ke-brand-400); outline-offset: 2px; }
  }
  &__ref-head { display: flex; align-items: center; gap: var(--ke-space-2); font-size: 12px; }
  &__ref-title { flex: 1; color: var(--ke-gray-700); }
  &__ref-score { color: var(--ke-gray-400); white-space: nowrap; }
  &__ref-snippet {
    margin: 6px 0 0; font-size: 12px; color: var(--ke-gray-500); line-height: 1.6;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  }
  &__ref-more { display: inline-block; margin-top: 4px; font-size: 12px; color: var(--ke-brand-600); }

  &__error {
    display: flex; align-items: center; gap: 6px; font-size: 13px;
    color: var(--ke-danger); background: var(--ke-danger-50);
    border: 1px solid var(--ke-danger-border); border-radius: var(--ke-radius-sm); padding: 6px 10px;
  }
  &__retry {
    margin-left: auto; border: 1px solid currentColor; background: transparent;
    color: var(--ke-danger); border-radius: var(--ke-radius-sm); padding: 2px 10px;
    font-size: 12px; cursor: pointer;
  }

  &__feedback { display: flex; align-items: center; gap: var(--ke-space-2); }
  &__fb-btn {
    display: inline-flex; align-items: center; gap: 4px;
    border: 1px solid var(--ke-gray-200); background: var(--ke-bg-card); color: var(--ke-gray-500);
    border-radius: var(--ke-radius-sm); padding: 3px 10px; font-size: 12px; cursor: pointer;
    &:hover { border-color: var(--ke-brand-400); color: var(--ke-brand-600); }
  }
  &__fb-done { font-size: 12px; }

  &__suggests {
    display: flex; flex-wrap: wrap; align-items: center; gap: var(--ke-space-2);
    margin-top: var(--ke-space-1);
  }
  &__suggests-label { font-size: 12px; color: var(--ke-gray-400); }
  &__suggest-chip {
    border: 1px solid var(--ke-gray-200); background: var(--ke-bg-card);
    color: var(--ke-gray-600); border-radius: 999px; padding: 4px 12px;
    font-size: 12px; cursor: pointer; line-height: 1.5;
    transition: border-color var(--ke-duration-fast), color var(--ke-duration-fast),
                background var(--ke-duration-fast);
    &:hover {
      border-color: var(--ke-brand-400); color: var(--ke-brand-600);
      background: var(--ke-brand-50);
    }
  }
}

.ref-dialog {
  &__head { display: flex; flex-direction: column; gap: 4px; padding-right: 24px; }
  &__title { font-size: 15px; font-weight: 600; color: var(--ke-gray-900); }
  &__meta { display: flex; gap: 6px; font-size: 12px; color: var(--ke-gray-400); }
  &__body { min-height: 120px; }
  &__crumb {
    display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
    margin-bottom: 10px; font-size: 12px; color: var(--ke-gray-500);
  }
  &__content {
    margin: 0; padding: 14px 16px; max-height: 52vh; overflow: auto;
    background: var(--ke-gray-50); border: 1px solid var(--ke-gray-200);
    border-radius: 8px;
    // MarkdownView 根容器 .md 在弹窗内收紧字号与行高，与正文区分
    :deep(.md) { font-size: 13px; line-height: 1.85; color: var(--ke-gray-700); }
  }
  &__empty { margin: 0; color: var(--ke-gray-400); }
  &__tip { margin: 0 0 10px; font-size: 12px; color: var(--ke-gray-400); }
}

@keyframes ke-blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }
@keyframes ke-dot {
  0%, 100% { opacity: 0.3; transform: translateY(0); }
  50%      { opacity: 1; transform: translateY(-2px); }
}
</style>
