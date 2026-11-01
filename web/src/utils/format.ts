import type { CacheStatus, DocumentStatus } from '@/types/common'

/** 文档状态 → 中文标签 */
export const DOC_STATUS_LABEL: Record<DocumentStatus, string> = {
  INIT: '初始化',
  UPLOADED: '已上传',
  CONVERTING: '转换中',
  CONVERTED: '已转换',
  CHUNKING: '切片中',
  CHUNKED: '已切片',
  VECTOR_STORED: '已就绪',
  FAILED: '处理失败',
}

/** 文档状态 → Element 标签类型（信息层级视觉编码） */
export const DOC_STATUS_TYPE: Record<DocumentStatus, 'info' | 'warning' | 'success' | 'danger'> = {
  INIT: 'info',
  UPLOADED: 'info',
  CONVERTING: 'warning',
  CONVERTED: 'warning',
  CHUNKING: 'warning',
  CHUNKED: 'warning',
  VECTOR_STORED: 'success',
  FAILED: 'danger',
}

/** 处理中的状态集合（用于轮询与禁用操作按钮） */
export const DOC_PROCESSING: DocumentStatus[] = ['UPLOADED', 'CONVERTING', 'CONVERTED', 'CHUNKING', 'CHUNKED']

/** 缓存状态 → 中文标签 */
export const CACHE_STATUS_LABEL: Record<CacheStatus, string> = {
  PENDING: '待审核',
  APPROVED: '已通过',
  ACTIVE: '已上线',
  REJECTED: '已驳回',
  DISABLED: '已下线',
  EXPIRED: '已过期',
}

/** 缓存状态 → Element 标签类型 */
export const CACHE_STATUS_TYPE: Record<CacheStatus, 'info' | 'warning' | 'success' | 'danger'> = {
  PENDING: 'warning',
  APPROVED: 'info',
  ACTIVE: 'success',
  REJECTED: 'danger',
  DISABLED: 'info',
  EXPIRED: 'info',
}

/** ISO 字符串 → 本地化日期时间；空值返回占位符 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 字节数 → 人类可读 */
export function formatBytes(bytes: number): string {
  if (bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  const val = bytes / Math.pow(1024, i)
  return `${val.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}
