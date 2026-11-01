/** 录音解码后的 WAV 编码工具：Float32 PCM → 16bit 单声道 WAV（16kHz） */

/** 目标采样率：ASR 上游对 16k 单声道适配最好，也能显著压缩 base64 体积 */
const TARGET_SAMPLE_RATE = 16000

/**
 * 把浏览器解码出的 AudioBuffer 编码为 WAV Blob。
 * 多声道取均值降为单声道，按线性插值重采样到 16kHz，小端 16bit PCM。
 */
export function audioBufferToWav(buffer: AudioBuffer): Blob {
  const channelData: Float32Array[] = []
  for (let ch = 0; ch < buffer.numberOfChannels; ch++) {
    channelData.push(buffer.getChannelData(ch))
  }
  const inputRate = buffer.sampleRate
  const outputRate = TARGET_SAMPLE_RATE
  const outputLength = Math.max(1, Math.floor(buffer.length * (outputRate / inputRate)))

  const pcm = new Int16Array(outputLength)
  for (let i = 0; i < outputLength; i++) {
    // 线性插值重采样
    const srcPos = (i / outputRate) * inputRate
    const idx = Math.floor(srcPos)
    const frac = srcPos - idx
    let sample = 0
    for (let ch = 0; ch < channelData.length; ch++) {
      const data = channelData[ch]
      const s0 = data[Math.min(idx, data.length - 1)]
      const s1 = data[Math.min(idx + 1, data.length - 1)]
      sample += s0 + (s1 - s0) * frac
    }
    sample /= channelData.length
    // 削波并转 16bit
    const clipped = Math.max(-1, Math.min(1, sample))
    pcm[i] = clipped < 0 ? clipped * 0x8000 : clipped * 0x7fff
  }

  const bytesPerSample = 2
  const blockAlign = bytesPerSample // 单声道
  const dataSize = pcm.byteLength
  const view = new DataView(new ArrayBuffer(44 + dataSize))

  const writeAscii = (offset: number, text: string): void => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i))
  }
  writeAscii(0, 'RIFF')
  view.setUint32(4, 36 + dataSize, true)
  writeAscii(8, 'WAVE')
  writeAscii(12, 'fmt ')
  view.setUint32(16, 16, true)                       // fmt 块大小
  view.setUint16(20, 1, true)                         // PCM 编码
  view.setUint16(22, 1, true)                         // 声道数
  view.setUint32(24, outputRate, true)
  view.setUint32(28, outputRate * blockAlign, true)   // 字节率
  view.setUint16(32, blockAlign, true)
  view.setUint16(34, 16, true)                        // 位深
  writeAscii(36, 'data')
  view.setUint32(40, dataSize, true)
  for (let i = 0; i < outputLength; i++) {
    view.setInt16(44 + i * 2, pcm[i], true)
  }
  return new Blob([view.buffer], { type: 'audio/wav' })
}
