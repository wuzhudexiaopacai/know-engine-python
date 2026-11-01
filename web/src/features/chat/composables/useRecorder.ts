import { onScopeDispose, ref } from 'vue'
import { audioBufferToWav } from '@/utils/audio'
import { transcribeAudio } from '@/api/speech'

/**
 * 麦克风录音 → WAV → 语音转写。
 *
 * 生命周期：start() 申请麦克风并开始录制；stop() 停止后自动走
 * decodeAudioData → 16k 单声道 WAV → /chat/speech/transcribe，
 * 结果通过 onTranscript 回调上抛（由调用方填入输入框）。
 * 浏览器不支持（无 MediaRecorder / 非安全上下文）时 supported=false。
 */
export function useRecorder(onTranscript: (text: string) => void) {
  const supported = typeof navigator !== 'undefined'
    && !!navigator.mediaDevices?.getUserMedia
    && typeof MediaRecorder !== 'undefined'

  const recording = ref(false)
  const transcribing = ref(false)

  let recorder: MediaRecorder | null = null
  let stream: MediaStream | null = null
  let chunks: Blob[] = []

  function releaseStream(): void {
    stream?.getTracks().forEach((t) => t.stop())
    stream = null
  }

  async function start(): Promise<void> {
    if (recording.value) return
    if (!supported) {
      throw new Error('当前浏览器不支持录音，请改用 Chrome / Edge')
    }
    stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    chunks = []
    recorder = new MediaRecorder(stream)
    recorder.ondataavailable = (e) => {
      if (e.data.size > 0) chunks.push(e.data)
    }
    // Chrome 默认 webm/opus，仅作解码中间格式
    recorder.start()
    recording.value = true
  }

  /** 停止并转写；无有效音频时静默返回（不产生空请求） */
  async function stop(): Promise<void> {
    if (!recording.value || !recorder) return
    recording.value = false

    const finished = new Promise<Blob>((resolve) => {
      recorder!.onstop = () => {
        resolve(new Blob(chunks, { type: recorder?.mimeType || 'audio/webm' }))
      }
    })
    recorder.stop()
    const blob = await finished
    releaseStream()
    if (blob.size < 1024) return   // 极短误触，直接丢弃

    transcribing.value = true
    try {
      const wav = await blobToWav(blob)
      const text = await transcribeAudio(wav)
      if (text.trim()) onTranscript(text.trim())
    } finally {
      transcribing.value = false
    }
  }

  /** 取消本次录音，不发起转写（录音中再点一次 mic 也可，此处供外部显式取消） */
  function cancel(): void {
    if (recorder && recorder.state !== 'inactive') {
      recorder.onstop = null
      recorder.stop()
    }
    recorder = null
    releaseStream()
    chunks = []
    recording.value = false
  }

  /** 容器解码上传格式 → 重采样编码为 16k 单声道 WAV */
  async function blobToWav(blob: Blob): Promise<Blob> {
    const ctx = new AudioContext()
    try {
      const buffer = await ctx.decodeAudioData(await blob.arrayBuffer())
      return audioBufferToWav(buffer)
    } finally {
      void ctx.close()
    }
  }

  // 组件销毁兜底：释放麦克风占用（录音指示灯常亮问题）
  onScopeDispose(() => cancel())

  return { supported, recording, transcribing, start, stop, cancel }
}
