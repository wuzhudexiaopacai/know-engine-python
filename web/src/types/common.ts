// 全局通用类型：统一响应封装、分页封装、枚举联合。

/** 后端统一响应体 R<T>（见 app/core/response.py） */
export interface BaseResult<T> {
  success: boolean
  code: number
  message: string
  data: T
}

/** 分页结果（后端约定 { total, page_num, page_size, records }） */
export interface PageResult<T> {
  total: number
  page_num: number
  page_size: number
  records: T[]
}

/** 文档生命周期状态（对齐 DocumentStatus） */
export type DocumentStatus =
  | 'INIT' | 'UPLOADED' | 'CONVERTING' | 'CONVERTED'
  | 'CHUNKING' | 'CHUNKED' | 'VECTOR_STORED' | 'FAILED'

/** 知识库类型（对齐 KnowledgeBaseType） */
export type KnowledgeBaseType = 'DOCUMENT_SEARCH' | 'DATA_QUERY'

/** 切片策略（对齐 SplitType） */
export type SplitType = 'LENGTH' | 'TITLE' | 'REGEX' | 'SEPARATOR' | 'SMART'

/** 问答缓存状态（对齐 CacheStatus） */
export type CacheStatus =
  | 'PENDING' | 'APPROVED' | 'ACTIVE' | 'REJECTED' | 'DISABLED' | 'EXPIRED'

/** 消息类型 */
export type MessageType = 'USER' | 'ASSISTANT'

/** 检索来源（对齐 RetrievalSource） */
export type RetrievalSource = 'vector' | 'fulltext' | 'hybrid' | 'sql' | 'graph' | 'qa_cache'

/** 身份：用户 / 管理员 */
export type Identity = 'user' | 'admin' | 'guest'
