import http from './http'
import type { BaseResult } from '@/types/common'

/**
 * 语音转写：前端把录音解码后重采样为 WAV（utils/audio），以 multipart 上传。
 * 独立于 JSON 封装，直接复用 axios 实例（拦截器自动带令牌与错误提示）。
 */
export async function transcribeAudio(wav: Blob): Promise<string> {
  const form = new FormData()
  form.append('file', wav, 'speech.wav')
  const res = await http.post<BaseResult<{ text: string }>>('/chat/speech/transcribe', form)
  return res.data.data.text
}
