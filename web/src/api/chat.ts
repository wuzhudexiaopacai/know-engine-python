import { del, get, post, put } from './http'
import type { Conversation, ChatMessageRecord, PublicModel, ReferenceDetail } from '@/types/chat'

/** 拉取会话列表；scope=all 仅管理员生效，可查看全部用户的会话 */
export function listConversations(scope: 'self' | 'all' = 'self'): Promise<Conversation[]> {
  return get<Conversation[]>('/chat/conversations', { scope })
}

/** 用户端可选模型（仅启用配置；空则回退环境变量默认模型） */
export function listChatModels(): Promise<PublicModel[]> {
  return get<PublicModel[]>('/chat/models')
}

/** 拉取指定会话的历史消息 */
export function listMessages(conversationId: string): Promise<ChatMessageRecord[]> {
  return get<ChatMessageRecord[]>('/chat/messages', { conversation_id: conversationId })
}

/** 删除会话 */
export function deleteConversation(conversationId: string): Promise<null> {
  return del<null>(`/chat/conversations/${encodeURIComponent(conversationId)}`)
}

/** 引用片段定位：返回文档、章节路径、段序与片段全文 */
export function getReferenceDetail(chunkId: string): Promise<ReferenceDetail> {
  return get<ReferenceDetail>(`/chat/references/${encodeURIComponent(chunkId)}`)
}

/** 答案点赞 / 点踩 */
export function submitFeedback(payload: {
  message_id: string
  helpful: boolean
  comment?: string | null
}): Promise<null> {
  return post<null>('/chat/feedback', payload)
}

/** 真中断：通知后端停止生成并保留部分答案 */
export function interruptChat(conversationId: string): Promise<{ interrupted: boolean }> {
  return post<{ interrupted: boolean }>('/chat/interrupt', { conversation_id: conversationId })
}

/** 对话图片上传：返回可回读的稳定地址（/api/files/...） */
export function uploadChatImage(file: File): Promise<{ url: string }> {
  const form = new FormData()
  form.append('file', file, file.name)
  return post<{ url: string }>('/chat/images', form)
}

/** 文生图请求体 */
export interface ImageGenRequest {
  prompt: string
  conversation_id?: string | null
  size?: string | null
  /** 一次性请求标识：中断时用它定位服务端仍在轮询的任务 */
  request_id?: string | null
}

/** 文生图结果：提问与图片 markdown 消息均已在后端落库 */
export interface ImageGenResult {
  conversation_id: string
  urls: string[]
  user_message: { message_id: string; role: string; content: string }
  assistant_message: { message_id: string; content: string }
}

/** 文生图：后端同步等待异步任务（数十秒），需放宽请求超时；signal 用于中断 */
export function generateImage(payload: ImageGenRequest, signal?: AbortSignal): Promise<ImageGenResult> {
  return post<ImageGenResult>('/chat/image', payload, { timeout: 150000, signal })
}

/** 中断进行中的文生图：服务端停止轮询且不落库（避免中断后刷新又出现图片） */
export function cancelImage(requestId: string): Promise<{ cancelled: boolean }> {
  return post<{ cancelled: boolean }>('/chat/image/cancel', { request_id: requestId })
}

/** 当前用户的对话偏好 */
export interface ChatPreferences {
  long_term_memory_enabled: boolean
  memory_window_size: number
  summary_round_interval: number
}

/** 获取当前用户对话偏好 */
export function getChatPreferences(): Promise<ChatPreferences> {
  return get<ChatPreferences>('/chat/preferences')
}

/** 更新当前用户对话偏好（仅传入字段生效） */
export function updateChatPreferences(payload: Partial<ChatPreferences>): Promise<ChatPreferences> {
  return put<ChatPreferences>('/chat/preferences', payload)
}

/** 获取基于用户记忆的新会话建议问题（3 条） */
export function getChatSuggestions(): Promise<string[]> {
  return get<string[]>('/chat/suggestions')
}
