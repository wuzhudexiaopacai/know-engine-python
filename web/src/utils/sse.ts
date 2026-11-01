import { getToken } from '@/api/http'
import type {
  RagReference,
  ResultCard,
  SsePayloadMap,
} from '@/types/chat'

/** 对话流式请求体 */
export interface StreamChatRequest {
  question: string
  conversation_id?: string | null
  use_cache?: boolean
  /** 管理员配置的对话模型 id，空则后端用默认模型 */
  model_id?: number | null
  /** 已上传图片的访问地址（/api/files/...），最多 4 张 */
  images?: string[]
}

/** 各 SSE 事件回调（均为可选，按需订阅） */
export interface StreamChatHandlers {
  onProgress?: (message: string) => void
  /** 会话 ID 就绪（新会话首个事件，可据此同步地址栏） */
  onConversation?: (conversationId: string) => void
  onToken?: (text: string) => void
  onReference?: (refs: RagReference[]) => void
  onCard?: (card: ResultCard) => void
  onAnswerMessage?: (messageId: string) => void
  onDone?: (payload: SsePayloadMap['done']) => void
  onError?: (message: string) => void
  /** 当前会话没有进行中的生成（仅 attach 接口会触发） */
  onIdle?: () => void
}

/**
 * 以 fetch + ReadableStream 消费后端 SSE（POST /api/chat/stream）。
 * 浏览器原生 EventSource 只支持 GET，无法携带令牌与 JSON body，故用 fetch 手动解帧。
 */
export async function streamChat(
  req: StreamChatRequest,
  handlers: StreamChatHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const base = import.meta.env.VITE_API_BASE_URL || '/api'
  const token = getToken()
  const resp = await fetch(`${base}/chat/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ ...req, conversation_id: req.conversation_id ?? null }),
    signal,
  })

  if (!resp.ok || !resp.body) {
    handlers.onError?.(`服务异常（HTTP ${resp.status}），请稍后重试`)
    return
  }
  await consumeSse(resp, handlers)
}

/**
 * 刷新后重连进行中的会话：GET /api/chat/stream/attach。
 * 后端无进行中任务时只发一个 idle 事件并关闭，前端保留历史消息。
 */
export async function attachChatStream(
  conversationId: string,
  handlers: StreamChatHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const base = import.meta.env.VITE_API_BASE_URL || '/api'
  const token = getToken()
  const resp = await fetch(
    `${base}/chat/stream/attach?conversation_id=${encodeURIComponent(conversationId)}`,
    {
      headers: {
        Accept: 'text/event-stream',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      signal,
    },
  )
  if (!resp.ok || !resp.body) return
  await consumeSse(resp, handlers)
}

/** 逐块读取响应体，按空行拆帧分发（初始流与重连流共用） */
async function consumeSse(resp: Response, handlers: StreamChatHandlers): Promise<void> {
  const reader = resp.body!.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let sep: number
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      dispatchFrame(frame, handlers)
    }
  }
  // 冲刷残余
  if (buffer.trim()) {
    dispatchFrame(buffer, handlers)
  }
}

/** 解析单帧：提取 event 名与 data JSON，分发到对应回调 */
function dispatchFrame(frame: string, handlers: StreamChatHandlers): void {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) {
      event = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      // 去掉 "data:" 前导（保留内容原始空格）
      dataLines.push(line.slice(5).replace(/^ /, ''))
    }
  }
  if (!dataLines.length) return
  const raw = dataLines.join('\n')
  let payload: unknown
  try {
    payload = JSON.parse(raw)
  } catch {
    return // 非 JSON 帧忽略，保证流不中断
  }

  switch (event) {
    case 'conversation':
      handlers.onConversation?.((payload as SsePayloadMap['conversation']).conversation_id)
      break
    case 'progress':
      handlers.onProgress?.((payload as SsePayloadMap['progress']).message)
      break
    case 'token':
      handlers.onToken?.((payload as SsePayloadMap['token']).text)
      break
    case 'reference':
      handlers.onReference?.((payload as SsePayloadMap['reference']).references)
      break
    case 'card':
      handlers.onCard?.((payload as SsePayloadMap['card']).card)
      break
    case 'answer_message':
      handlers.onAnswerMessage?.((payload as SsePayloadMap['answer_message']).message_id)
      break
    case 'done':
      handlers.onDone?.(payload as SsePayloadMap['done'])
      break
    case 'error':
      handlers.onError?.((payload as SsePayloadMap['error']).message)
      break
    case 'idle':
      handlers.onIdle?.()
      break
    default:
      break
  }
}
