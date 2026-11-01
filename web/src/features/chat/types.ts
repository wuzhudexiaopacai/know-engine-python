import type { RagReference, ResultCard } from '@/types/chat'

/** 前端对话消息视图模型：融合历史消息与实时流式产出的中间态 */
export interface UiMessage {
  /** 后端 message_id；流式生成中占位为空串，answer_message 事件回填 */
  id: string
  role: 'user' | 'assistant'
  content: string
  /** 用户消息去掉图片 markdown 后的文字部分（图片消息拆分展示用） */
  text?: string
  /** 用户侧生成的图片附件（历史与实时消息均从 content 提取） */
  images?: string[]
  /** 是否仍在流式接收 */
  streaming: boolean
  /** 当前管道阶段进度文案（assistant 流式期展示） */
  progress?: string
  references: RagReference[]
  card?: ResultCard | null
  /** 改写后的检索问题（可选展示） */
  transformContent?: string | null
  cacheHit?: boolean
  helpful?: number | null
  /** 生成该答案所用的模型展示名 */
  modelName?: string | null
  /** 回答结束后的 follow-up 建议问题（点击填入输入框） */
  suggestions?: string[]
  /** 管道级错误提示 */
  error?: string
  createdAt: number
}
