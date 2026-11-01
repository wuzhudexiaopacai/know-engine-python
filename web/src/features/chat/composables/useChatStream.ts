import { computed, onScopeDispose, ref } from 'vue'
import { attachChatStream, streamChat } from '@/utils/sse'
import { interruptChat } from '@/api/chat'
import type { UiMessage } from '../types'

/** 发起一次流式问答的入参 */
export interface SendOptions {
  question: string
  conversationId: string | null
  useCache: boolean
  /** 用户选择的模型 id；null 表示用默认模型 */
  modelId?: number | null
  /** 本轮附带的图片地址（已上传） */
  images?: string[]
}

/** 流结束钩子：回传后端确定的会话 ID，供上层同步当前会话 */
export interface SendHooks {
  onDone?: (conversationId: string) => void
  /** 会话 ID 一就绪即回调（新会话无需等回答结束即可写入地址栏） */
  onConversation?: (conversationId: string) => void
  /** 回答结束后的 follow-up 建议问题 */
  onSuggestions?: (questions: string[]) => void
}

/** 判定 AbortError（用户主动停止 vs 真实网络异常） */
function isAbortError(e: unknown): boolean {
  return e instanceof DOMException && e.name === 'AbortError'
}

/**
 * 封装 SSE 流式对话到"单条助手消息"的反应式更新。
 *
 * 设计取舍：UI 消息数组由上层 ChatView 持有，本 composable 只负责把流事件写进
 * 传入的目标消息对象（该对象须为上层响应式数组中的引用），从而保持职责单一、
 * 便于测试，且避免把整段会话状态耦合进 composable。
 */
export function useChatStream() {
  const controller = ref<AbortController | null>(null)
  /** 当前活跃会话 ID（done 前不可知，新会话中断仅能断开前端连接） */
  let activeConvId = ''
  /** 是否正在流式接收（驱动输入区禁用与停止按钮显隐） */
  const streaming = computed(() => controller.value !== null)

  async function send(opts: SendOptions, target: UiMessage, hooks?: SendHooks): Promise<void> {
    // 复位目标消息的流式态，保证重试从干净状态开始
    target.content = ''
    target.error = undefined
    target.progress = undefined
    target.references = []
    target.card = null
    target.streaming = true

    const abort = new AbortController()
    controller.value = abort
    activeConvId = opts.conversationId ?? ''
    try {
      await streamChat(
        {
          question: opts.question,
          conversation_id: opts.conversationId,
          use_cache: opts.useCache,
          model_id: opts.modelId ?? null,
          images: opts.images ?? [],
        },
        {
          onConversation: (conversationId) => {
            activeConvId = conversationId
            hooks?.onConversation?.(conversationId)
          },
          onProgress: (message) => {
            target.progress = message
          },
          onToken: (text) => {
            // 首 token 到达即隐藏进度占位
            target.progress = undefined
            target.content += text
          },
          onReference: (references) => {
            target.references = references
          },
          onCard: (card) => {
            target.card = card
          },
          onAnswerMessage: (messageId) => {
            if (messageId) target.id = messageId
          },
          onSuggestions: (questions) => {
            hooks?.onSuggestions?.(questions)
          },
          onDone: (payload) => {
            target.streaming = false
            activeConvId = payload?.conversation_id || activeConvId
            hooks?.onDone?.(payload?.conversation_id ?? '')
          },
          onError: (message) => {
            target.error = message
            target.streaming = false
          },
        },
        abort.signal,
      )
    } catch (e) {
      // 主动停止不算异常，保留已生成内容；真实异常才提示错误
      if (!isAbortError(e)) {
        target.error = '连接中断，请稍后重试'
      }
    } finally {
      target.streaming = false
      target.progress = undefined
      controller.value = null
    }
  }

  /**
   * 刷新/重新进入进行中的会话：回放并继续接收服务端事件。
   * 只有真正收到事件才清空历史里已落库的部分答案，避免已完成会话被重放覆盖。
   */
  async function resume(conversationId: string, target: UiMessage,
                        hooks?: SendHooks): Promise<void> {
    const abort = new AbortController()
    controller.value = abort
    activeConvId = conversationId
    // 先不置 streaming：若服务端早已结束只会回 idle，避免已完成会话闪一下“思考中”
    let cleared = false
    const resetForReplay = (): void => {
      if (cleared) return
      cleared = true
      target.content = ''
      target.references = []
      target.card = null
      target.progress = undefined
      target.error = undefined
      target.streaming = true
    }
    try {
      await attachChatStream(conversationId, {
        onProgress: (message) => { resetForReplay(); target.progress = message },
        onToken: (text) => {
          resetForReplay()
          target.progress = undefined
          target.content += text
        },
        onReference: (references) => { resetForReplay(); target.references = references },
        onCard: (card) => { resetForReplay(); target.card = card },
        onAnswerMessage: (messageId) => { if (messageId) target.id = messageId },
        onSuggestions: (questions) => { hooks?.onSuggestions?.(questions) },
        onDone: (payload) => {
          target.streaming = false
          hooks?.onDone?.(payload?.conversation_id ?? conversationId)
        },
        onError: (message) => { target.error = message; target.streaming = false },
        onIdle: () => { target.streaming = false },
      }, abort.signal)
    } catch (e) {
      if (!isAbortError(e)) target.error = '连接中断，请稍后重试'
    } finally {
      target.streaming = false
      target.progress = undefined
      controller.value = null
    }
  }

  /** 离开会话时断开续流连接（不触发后端真中断，生成继续在服务端跑） */
  function cancel(): void {
    controller.value?.abort()
    controller.value = null
  }

  /**
   * 用户主动停止：优先通知后端真中断（后端断开上游 LLM 流、持久化部分答案后
   * 正常发 done 收尾）；3s 内流未自然关闭或会话 ID 未知时，兜底断开前端连接。
   */
  function stop(): void {
    const current = controller.value
    if (!current) return
    if (!activeConvId) {
      current.abort()
      return
    }
    void interruptChat(activeConvId).catch(() => { /* 中断失败不阻塞停止体验 */ })
    const timer = window.setTimeout(() => current.abort(), 3000)
    current.signal.addEventListener('abort', () => window.clearTimeout(timer), { once: true })
  }

  // 组件卸载时兜底中断，避免遗留悬挂的连接
  onScopeDispose(() => controller.value?.abort())

  return { streaming, send, stop, resume, cancel }
}
