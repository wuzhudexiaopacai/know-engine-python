import type { CacheStatus } from './common'

/** 问答缓存条目（对齐后端 _entry_to_dict） */
export interface QaCacheEntry {
  id: string
  question: string
  answer: string
  status: CacheStatus
  frequency: number
  hits: number
  quality: number
  reviewer: string | null
  review_note: string | null
  disable_reason: string | null
  created_at: string | null
  activated_at: string | null
}

/** 审核动作负载 */
export interface ReviewActionPayload {
  note?: string | null
}
