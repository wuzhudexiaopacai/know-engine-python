import SparkMD5 from 'spark-md5'

/** 分片读取的块大小：2MB，兼顾大文件内存占用与计算速度 */
const READ_BLOCK = 2 * 1024 * 1024

/**
 * 增量计算整文件 MD5（hex，32 位）。
 * 采用分片读取 + SparkMD5.append 累积，避免一次性把大文件读进内存；
 * 结果作为服务端"秒传"与跨版本去重的身份键。
 *
 * @param file      目标文件
 * @param onProgress 进度回调，参数为 0~1 的比例
 */
export function computeFileMd5(file: Blob, onProgress?: (ratio: number) => void): Promise<string> {
  return new Promise((resolve, reject) => {
    const spark = new SparkMD5.ArrayBuffer()
    const reader = new FileReader()
    const total = file.size
    let cursor = 0

    reader.onload = (e) => {
      spark.append(e.target?.result as ArrayBuffer)
      cursor += READ_BLOCK
      onProgress?.(Math.min(cursor / total, 1))
      if (cursor < total) {
        loadNext()
      } else {
        resolve(spark.end())
      }
    }
    reader.onerror = () => reject(reader.error ?? new Error('文件读取失败'))

    function loadNext(): void {
      const slice = file.slice(cursor, Math.min(cursor + READ_BLOCK, total))
      reader.readAsArrayBuffer(slice)
    }

    loadNext()
  })
}

/** 上传分片的推荐大小：5MB（与服务端上限协同，前端按此切块） */
export const UPLOAD_CHUNK_SIZE = 5 * 1024 * 1024

/** 计算单个分片的 MD5（用于服务端可选的完整性校验） */
export async function computeChunkMd5(chunk: Blob): Promise<string> {
  const buf = await chunk.arrayBuffer()
  return SparkMD5.ArrayBuffer.hash(buf)
}
