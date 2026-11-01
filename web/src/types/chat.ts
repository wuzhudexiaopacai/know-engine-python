import type { MessageType, RetrievalSource } from './common'

/** RAG 引用溯源（后端 RagReference.to_dict，camelCase） */
export interface RagReference {
  documentId: string
  documentTitle: string | null
  chunkId: string | null
  version: string | null
  chunkContent: string
  similarityScore: number | null
  retrievalSource: RetrievalSource
  rerankScore: number | null
}

/** 引用片段定位详情（GET /api/chat/references/{chunkId}） */
export interface ReferenceDetail {
  documentId: string
  documentTitle: string
  version: string | null
  chunkId: string
  /** 章节标题路径，如「一级标题 > 二级标题」 */
  sectionPath: string
  /** 当前段序号（从 1 开始） */
  order: number
  /** 该版本总段数 */
  total: number
  content: string
  /** 片段过长时后端只返回前 4000 字 */
  truncated: boolean
}

/** 用户端可选模型（后端 /api/chat/models，不暴露地址与密钥） */
export interface PublicModel {
  id: number
  display_name: string
  model_name: string
  provider: string
  is_default: number
}

/** 会话摘要 */
export interface Conversation {
  conversation_id: string
  title: string | null
  status: string
  created_at: string
  /** 会话归属键 identity:sub（管理员查看全部会话时用于区分归属） */
  owner?: string
}

/** 历史消息（后端 _message_to_dict） */
export interface ChatMessageRecord {
  message_id: string
  type: MessageType
  content: string | null
  transform_content: string | null
  rag_references: RagReference[]
  cache_hit: number
  helpful: number | null
  /** 生成该答案所用的模型展示名 */
  model_name: string | null
  created_at: string
}

/** 结构化卡片（Text2SQL / Text2Cypher 产出的对比表） */
export interface ResultCard {
  type: string
  title: string
  /** 列名；LLM 协议卡可能缺省，前端按 data 首行键兜底推导 */
  columns?: string[]
  data: Array<Record<string, unknown>>
}

/** SSE 事件名（与后端 SseEvent 对齐） */
export type SseEventName =
  | 'progress' | 'conversation' | 'token' | 'reference' | 'card'
  | 'answer_message' | 'done' | 'error' | 'idle'

/** 各事件 data 负载 */
export interface SsePayloadMap {
  progress: { message: string }
  conversation: { conversation_id: string }
  token: { text: string }
  reference: { references: RagReference[] }
  card: { card: ResultCard }
  answer_message: { message_id: string }
  done: { conversation_id: string; message_id: string }
  error: { message: string }
  idle: { conversation_id: string }
}
