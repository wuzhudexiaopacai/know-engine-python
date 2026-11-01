import type { DocumentStatus, KnowledgeBaseType, SplitType } from './common'

/** 流水线阶段：转换 / 切片 / 向量化 */
export type PipelineStage = 'convert' | 'split' | 'embedding'

/** 阶段轨迹状态（与后端 pipeline 留痕常量一致） */
export type PipelineStageStatus = 'STARTED' | 'SUCCEEDED' | 'FAILED'

/** 阶段轨迹单条记录 */
export interface PipelineStageRecord {
  stage: PipelineStage
  status: PipelineStageStatus
  at: string
  detail: string
}

/** 单版本扩展信息：阶段轨迹 + 失败原因 + 自动补偿次数 */
export interface VersionExtension {
  stages: PipelineStageRecord[]
  attempts: number
  md5?: string
  version?: string
  lastError?: string
  failedStage?: PipelineStage
  failedAt?: string
}

/** 文档扩展信息（后端 extension JSON） */
export interface DocumentExtension {
  splitConfig?: SplitConfig
  fileType?: string
  version?: string
  fileMd5s?: string[]
  versions?: Record<string, VersionExtension>
}

/** 分段（对齐 SegmentVO）：正文 + 父子关系 + 向量化摘要 */
export interface SegmentVO {
  chunk_id: string
  text: string
  chunk_order: number
  status: string | null
  skip_embedding: number
  parent_chunk_id: string | null
  brother_chunk_id: string | null
  embedding_dim: number
  vector_norm: number | null
  vector_preview: number[]
  metadata: Record<string, unknown> | null
}

/** 切片/向量化结果（对齐 SegmentListVO） */
export interface SegmentListVO {
  total: number
  page: number
  page_size: number
  embedded_count: number
  skipped_count: number
  vector_dim: number
  records: SegmentVO[]
}

/** 文档视图对象（对齐 DocumentVO） */
export interface DocumentVO {
  doc_id: number
  doc_title: string
  status: DocumentStatus
  knowledge_base_type: KnowledgeBaseType
  description: string | null
  accessible_by: string | null
  expire_date: string | null
  current_version_id: number | null
  current_version: string | null
  segment_count: number
  lock_version: number
  extension: DocumentExtension | null
  created_at: string
  updated_at: string
}

/** 文档版本视图对象（对齐 DocumentVersionVO） */
export interface DocumentVersionVO {
  version_id: number
  doc_id: number
  version: string
  status: DocumentStatus
  changelog: string | null
  upload_user: string | null
  doc_url: string | null
  converted_doc_url: string | null
  content_hash: string | null
  lock_version: number
  created_at: string
}

/** 分片上传初始化请求（对齐 ChunkInitRequest） */
export interface ChunkInitPayload {
  file_name: string
  file_size: number
  file_md5: string
  chunk_size?: number
  doc_id?: number | null
  changelog?: string | null
}

/** 分片上传初始化响应（对齐 ChunkInitResponse） */
export interface ChunkInitResult {
  upload_id?: string | null
  uploaded_chunks: number[]
  chunk_size: number
  instant_hit: boolean
  doc_id?: number | null
  version_id?: number | null
}

/** 上传进度对账（对齐 upload/status 返回） */
export interface UploadStatus {
  upload_id: string
  uploaded_chunks: number[]
  total_chunks: number
  percent: number
}

/** 分片合并入库请求（对齐 ChunkFinalizeRequest） */
export interface FinalizePayload {
  upload_id: string
  split_type: SplitType
  chunk_size: number
  overlap: number
  title_level: number
  regex?: string | null
  separator?: string | null
  knowledge_base_type: KnowledgeBaseType
  accessible_by?: string | null
  expire_date?: string | null
  description?: string | null
}

/** finalize 返回 */
export interface FinalizeResult {
  doc_id: number
  version_id: number
  file_type: string
  doc_url: string
}

/** 重新切片配置（对齐 SplitConfigUpdate） */
export interface SplitConfig {
  split_type: SplitType
  chunk_size: number
  overlap: number
  title_level: number
  regex?: string | null
  separator?: string | null
}
