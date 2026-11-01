import { get, post, del, getToken, default as http } from './http'
import type { PageResult } from '@/types/common'
import type {
  ChunkInitPayload,
  ChunkInitResult,
  FinalizePayload,
  FinalizeResult,
  DocumentVO,
  DocumentVersionVO,
  SegmentListVO,
  SplitConfig,
  UploadStatus,
} from '@/types/document'

// ---------------- 分片上传 ----------------
export function uploadInit(payload: ChunkInitPayload): Promise<ChunkInitResult> {
  return post<ChunkInitResult>('/document/upload/init', payload)
}

/** 上传单个分片：multipart/form-data */
export function uploadChunk(uploadId: string, index: number, blob: Blob, chunkMd5?: string): Promise<{ uploaded: number; received: number; total: number }> {
  const form = new FormData()
  form.append('upload_id', uploadId)
  form.append('index', String(index))
  if (chunkMd5) form.append('chunk_md5', chunkMd5)
  // 后端以 file 字段接收分片二进制（文件名供类型魔数检测）
  form.append('file', blob, `chunk_${index}`)
  return http
    .post<{ data: { uploaded: number; received: number; total: number } }>('/document/upload/chunk', form)
    .then((res) => res.data.data)
}

export function uploadStatus(uploadId: string): Promise<UploadStatus> {
  return get<UploadStatus>('/document/upload/status', { upload_id: uploadId })
}

export function uploadFinalize(payload: FinalizePayload): Promise<FinalizeResult> {
  return post<FinalizeResult>('/document/upload/finalize', payload)
}

// ---------------- 文档管理 ----------------
export function listDocuments(params: { page: number; size: number; status?: string; keyword?: string }): Promise<PageResult<DocumentVO>> {
  return get<PageResult<DocumentVO>>('/document/list', params)
}

export function getDocument(docId: number): Promise<DocumentVO> {
  return get<DocumentVO>(`/document/${docId}`)
}

export function listVersions(docId: number): Promise<DocumentVersionVO[]> {
  return get<DocumentVersionVO[]>(`/document/${docId}/versions`)
}

export function switchVersion(docId: number, versionId: number, lockVersion: number): Promise<null> {
  return post<null>(`/document/${docId}/switch-version?version_id=${versionId}&lock_version=${lockVersion}`)
}

export function deleteVersion(docId: number, versionId: number): Promise<null> {
  return post<null>(`/document/${docId}/delete-version?version_id=${versionId}`)
}

export function deleteDocument(docId: number, lockVersion: number): Promise<null> {
  return del<null>(`/document/${docId}?lock_version=${lockVersion}`)
}

export function retryPipeline(docId: number, versionId: number, stage: string): Promise<null> {
  return post<null>(`/document/${docId}/retry?version_id=${versionId}&stage=${encodeURIComponent(stage)}`)
}

export function resplit(docId: number, versionId: number, config: SplitConfig): Promise<null> {
  return post<null>(`/document/${docId}/resplit?version_id=${versionId}`, config)
}

// ---------------- 切片 / 向量化结果 ----------------
export function listSegments(docId: number, versionId: number,
                             page = 1, size = 10): Promise<SegmentListVO> {
  return get<SegmentListVO>(`/document/${docId}/versions/${versionId}/segments`, { page, size })
}

// ---------------- 文件预览 ----------------
/**
 * 预览地址：带 token 查询参数，供 iframe / 新窗口直接打开
 * （浏览器直开不会带 Authorization 请求头，后端 require_admin 兼容 ?token= 鉴权）。
 */
export function previewUrl(docId: number, versionId: number, kind: 'converted' | 'original' = 'converted'): string {
  const base = import.meta.env.VITE_API_BASE_URL || '/api'
  const params = new URLSearchParams({ kind })
  const token = getToken()
  if (token) params.set('token', token)
  return `${base}/document/${docId}/versions/${versionId}/preview?${params.toString()}`
}

/** 下载地址：后端以 attachment 返回，浏览器直接落盘而不是内嵌预览 */
export function downloadUrl(docId: number, versionId: number,
                            kind: 'converted' | 'original' = 'converted'): string {
  const base = import.meta.env.VITE_API_BASE_URL || '/api'
  const params = new URLSearchParams({ kind, download: '1' })
  const token = getToken()
  if (token) params.set('token', token)
  return `${base}/document/${docId}/versions/${versionId}/preview?${params.toString()}`
}

/** Markdown 打包下载：ZIP 内含 .md 与 images/（图片用相对路径，离线可看图） */
export function bundleUrl(docId: number, versionId: number,
                          kind: 'converted' | 'original' = 'converted'): string {
  const base = import.meta.env.VITE_API_BASE_URL || '/api'
  const params = new URLSearchParams({ kind })
  const token = getToken()
  if (token) params.set('token', token)
  return `${base}/document/${docId}/versions/${versionId}/preview/bundle?${params.toString()}`
}

/** 预览渲染数据：Word 转 Markdown、Excel/CSV 转二维表，PDF/图片交给 iframe */
export interface PreviewSheet {
  name: string
  rows: string[][]
  truncated: boolean
}

export interface PreviewContent {
  mode: 'markdown' | 'sheet' | 'file' | 'unsupported'
  text?: string
  sheets?: PreviewSheet[]
  filename?: string
}

export async function fetchPreviewContent(docId: number, versionId: number,
                                           kind: 'converted' | 'original' = 'converted'): Promise<PreviewContent> {
  return get<PreviewContent>(`/document/${docId}/versions/${versionId}/preview/content`, { kind })
}
