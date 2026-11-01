import { get, post } from './http'
import type { PageResult } from '@/types/common'
import type { QaCacheEntry } from '@/types/qaCache'

export function listEntries(params: { status?: string; page: number; size: number }): Promise<PageResult<QaCacheEntry>> {
  return get<PageResult<QaCacheEntry>>('/qa-cache/entries', params)
}

/** 通过审核并上线 */
export function approveEntry(entryId: string, note?: string): Promise<QaCacheEntry> {
  return post<QaCacheEntry>(`/qa-cache/entries/${encodeURIComponent(entryId)}/approve`, { note: note ?? null })
}

/** 驳回 */
export function rejectEntry(entryId: string, note?: string): Promise<QaCacheEntry> {
  return post<QaCacheEntry>(`/qa-cache/entries/${encodeURIComponent(entryId)}/reject`, { note: note ?? null })
}

/** 下线（手动/其他原因） */
export function disableEntry(entryId: string, reason = 'MANUAL', detail?: string): Promise<QaCacheEntry> {
  const q = `reason=${encodeURIComponent(reason)}${detail ? `&detail=${encodeURIComponent(detail)}` : ''}`
  return post<QaCacheEntry>(`/qa-cache/entries/${encodeURIComponent(entryId)}/disable?${q}`)
}
