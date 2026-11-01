import { computed, ref } from 'vue'
import { uploadInit, uploadChunk, uploadFinalize } from '@/api/document'
import { computeFileMd5, computeChunkMd5, UPLOAD_CHUNK_SIZE } from '@/utils/hash'
import type { FinalizePayload } from '@/types/document'

/** 上传阶段：待命 / 计算哈希 / 上传分片 / 合并入库 / 完成 */
export type UploadPhase = 'idle' | 'hashing' | 'uploading' | 'finalizing' | 'done'

/** finalize 之前的切片与入库配置（由上层表单传入） */
export type UploadConfig = Omit<FinalizePayload, 'upload_id'>

/** 进度快照 */
export interface UploadProgress {
  phase: UploadPhase
  /** 0~100 整文件哈希进度 */
  hashPercent: number
  /** 0~100 分片上传进度 */
  uploadPercent: number
  uploaded: number
  total: number
  /** 秒传命中标记 */
  instantHit: boolean
}

export interface UploadResult {
  docId: number | null
  versionId: number | null
  instantHit: boolean
}

/**
 * 大文件分片上传编排：
 *  1. 增量计算整文件 MD5（秒传键）
 *  2. init 申请上传会话，拿到已传分片（断点续传）与秒传结果
 *  3. 逐片上传缺失分片，累计进度，支持中断
 *  4. finalize 触发合并入库 + 切片向量化
 *
 * 失败即抛出，由调用方（对话框）统一捕获提示，本层不做静默降级。
 */
export function useChunkedUpload() {
  const progress = ref<UploadProgress>({
    phase: 'idle', hashPercent: 0, uploadPercent: 0,
    uploaded: 0, total: 0, instantHit: false,
  })
  const busy = computed(() => progress.value.phase !== 'idle' && progress.value.phase !== 'done')
  const controller = ref<AbortController | null>(null)

  function reset(): void {
    progress.value = {
      phase: 'idle', hashPercent: 0, uploadPercent: 0,
      uploaded: 0, total: 0, instantHit: false,
    }
  }

  function cancel(): void {
    controller.value?.abort()
    progress.value.phase = 'idle'
  }

  async function start(file: File, config: UploadConfig, docId?: number | null): Promise<UploadResult> {
    reset();
    controller.value = new AbortController()
    const signal = controller.value.signal

    try {
      // 1) 整文件 MD5
      progress.value.phase = 'hashing'
      const fileMd5 = await computeFileMd5(file, (r) => { progress.value.hashPercent = Math.round(r * 100) })

      // 2) init
      const init = await uploadInit({
        file_name: file.name,
        file_size: file.size,
        file_md5: fileMd5,
        chunk_size: UPLOAD_CHUNK_SIZE,
        doc_id: docId ?? null,
      })

      if (init.instant_hit) {
        progress.value.instantHit = true
        progress.value.phase = 'done'
        return { docId: init.doc_id ?? null, versionId: init.version_id ?? null, instantHit: true }
      }

      const uploadId = init.upload_id
      if (!uploadId) throw new Error('上传会话初始化失败')

      // 3) 分片上传（跳过已传，支持断点续传）
      const chunkSize = init.chunk_size || UPLOAD_CHUNK_SIZE
      const totalChunks = Math.ceil(file.size / chunkSize)
      const done = new Set(init.uploaded_chunks ?? [])
      progress.value.total = totalChunks
      progress.value.uploaded = done.size
      progress.value.uploadPercent = totalChunks ? Math.round((done.size / totalChunks) * 100) : 0
      progress.value.phase = 'uploading'

      for (let i = 0; i < totalChunks; i++) {
        if (signal.aborted) throw new DOMException('已取消', 'AbortError')
        if (done.has(i)) continue

        const start = i * chunkSize
        const blob = file.slice(start, Math.min(start + chunkSize, file.size))
        const chunkMd5 = await computeChunkMd5(blob)
        await uploadChunk(uploadId, i, blob, chunkMd5)

        progress.value.uploaded = i + 1
        progress.value.uploadPercent = Math.round(((i + 1) / totalChunks) * 100)
      }

      // 4) finalize 合并入库
      progress.value.phase = 'finalizing'
      const fin = await uploadFinalize({ upload_id: uploadId, ...config })
      progress.value.phase = 'done'
      return { docId: fin.doc_id, versionId: fin.version_id, instantHit: false }
    } finally {
      controller.value = null
    }
  }

  return { progress, busy, start, cancel, reset }
}
