<script setup lang="ts">
// Markdown 正文渲染：markdown-it + KaTeX 公式 + Mermaid 图表 + DOMPurify 消毒
// 安全红线：LLM 输出不可信，v-html 前必须经 DOMPurify 白名单消毒
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import type { MarkdownIt as MarkdownItInstance, RendererRule } from 'markdown-it'
import DOMPurify from 'dompurify'
import markdownItKatex from '@vscode/markdown-it-katex'
import 'katex/dist/katex.min.css'

const props = defineProps<{ text: string; streaming?: boolean }>()

// Mermaid 体积较大，按需动态加载：只有出现 mermaid 代码块时才拉取
let mermaidPromise: Promise<typeof import('mermaid').default> | null = null
function loadMermaid() {
  if (!mermaidPromise) {
    mermaidPromise = import('mermaid').then((m) => {
      m.default.initialize({
        startOnLoad: false,
        securityLevel: 'strict',   // 图表内不执行脚本，防注入
        theme: 'default',
        fontFamily: 'inherit',
      })
      return m.default
    })
  }
  return mermaidPromise
}

const md: MarkdownItInstance = new MarkdownIt({
  html: false,        // 禁原始 HTML，仅渲染 markdown 安全子集
  linkify: true,      // 自动识别裸链接
  breaks: true,       // 单换行按 <br> 处理（LLM 输出常见）
  typographer: false,
}).use(markdownItKatex, { errorColor: '#dc2626', throwOnError: false })

// 代码块右上角注入复制按钮（纯 CSS + 事件委托，无内联脚本）
// mermaid 代码块：流式期仍按普通代码块展示（源码不完整无法渲染），结束后替换为图表占位
const defaultFence: RendererRule = md.renderer.rules.fence
md.renderer.rules.fence = ((tokens, idx, options, env, renderer) => {
  const token = tokens[idx]
  const lang = (token.info || '').trim().split(/\s+/)[0].toLowerCase()
  if (lang === 'mermaid' && !(env as { streaming?: boolean } | undefined)?.streaming) {
    return `<div class="md__mermaid" data-graph="${encodeURIComponent(token.content)}"></div>`
  }
  const encoded = encodeURIComponent(token.content)
  // 包一层 relative 容器：按钮作为 pre 的兄弟但同一包裹内，悬停任意位置可复制
  return (
    `<div class="md__pre">${defaultFence(tokens, idx, options, env, renderer)}` +
    `<button type="button" class="md__copy" data-copy="${encoded}" aria-label="复制代码">复制</button></div>`
  )
}) satisfies RendererRule

// 历史消息防御：旧库消息未经后端卡片剥离，渲染前去掉 [CARD:{...}] 协议块，防裸 JSON 直出。
// 与后端 _extract_cards 同构的字符串感知花括号配对（JSON 内可嵌套 {} / 字符串含 }）。
const CARD_MARK = '[CARD:'
function stripCardBlocks(text: string): string {
  if (!text.includes(CARD_MARK)) return text
  let out = ''
  let i = 0
  while (i < text.length) {
    const pos = text.indexOf(CARD_MARK, i)
    if (pos < 0) {
      out += text.slice(i)
      break
    }
    out += text.slice(i, pos)
    const start = text.indexOf('{', pos)
    if (start < 0) break                       // 标记后无 JSON 主体：丢弃尾部
    let depth = 0
    let inStr = false
    let esc = false
    let j = start
    for (; j < text.length; j++) {
      const ch = text[j]
      if (inStr) {
        if (esc) esc = false
        else if (ch === '\\') esc = true
        else if (ch === '"') inStr = false
      } else if (ch === '"') inStr = true
      else if (ch === '{') depth++
      else if (ch === '}') {
        depth--
        if (depth === 0) break
      }
    }
    if (depth !== 0) break                      // 未闭合：丢弃残块
    i = j + 1
    if (text[i] === ']') i++                    // 跳过紧随的 ] 收尾
  }
  return out
}

const html = computed(() =>
  DOMPurify.sanitize(
    md.render(stripCardBlocks(props.text ?? ''), { streaming: props.streaming }),
    { ADD_ATTR: ['data-copy', 'data-graph'] },
  ),
)

// 流式期节流重渲染（最多 ~10 次/秒），完成后立即出最终结果
const viewHtml = ref(html.value)
let timer: ReturnType<typeof setInterval> | null = null
function stopThrottle() {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}
watch(html, (next) => {
  if (!props.streaming) {
    stopThrottle()
    viewHtml.value = next
    return
  }
  if (timer !== null) return
  timer = setInterval(() => {
    stopThrottle()
    viewHtml.value = html.value
  }, 100)
})
watch(
  () => props.streaming,
  (streaming) => {
    if (!streaming) {
      stopThrottle()
      viewHtml.value = html.value
    }
  },
)
onBeforeUnmount(stopThrottle)

// ---------------- Mermaid 渲染 ----------------
// 渲染是异步的（mermaid 动态加载 + 布局计算），v-html 更新后等 DOM 落地再画
const rootEl = ref<HTMLElement | null>(null)
let mermaidSeq = 0

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c] as string))
}

async function renderMermaid() {
  const root = rootEl.value
  if (!root || props.streaming) return        // 流式期源码不完整，画了也必然是错的
  const pending = Array.from(root.querySelectorAll<HTMLElement>('.md__mermaid'))
    .filter((el) => el.dataset.rendered !== '1')
  if (!pending.length) return
  let mermaid: typeof import('mermaid').default
  try {
    mermaid = await loadMermaid()
  } catch {
    return
  }
  for (const el of pending) {
    const code = decodeURIComponent(el.dataset.graph ?? '')
    if (!el.isConnected || !code.trim()) continue
    el.dataset.rendered = '1'                 // 先标记，防止 watch 抖动时重复渲染
    try {
      await mermaid.parse(code)               // 先校验语法，避免 render 抛出并留下脏节点
      const { svg } = await mermaid.render(`md-mermaid-${++mermaidSeq}`, code)
      el.innerHTML = svg
    } catch {
      el.innerHTML =
        '<div class="md__mermaid-error">图表语法解析失败，已显示原始代码</div>' +
        `<pre>${escapeHtml(code)}</pre>`
    }
  }
}

watch(viewHtml, async () => {
  await nextTick()
  void renderMermaid()
})
onMounted(() => {
  void renderMermaid()
})

async function onCopyClick(e: MouseEvent) {
  const btn = (e.target as HTMLElement | null)?.closest?.('.md__copy')
  if (!btn) return
  const code = decodeURIComponent(btn.getAttribute('data-copy') ?? '')
  try {
    await navigator.clipboard.writeText(code)
    btn.textContent = '已复制'
  } catch {
    btn.textContent = '复制失败'
  }
  window.setTimeout(() => {
    btn.textContent = '复制'
  }, 1500)
}
</script>

<template>
  <div ref="rootEl" class="md" v-html="viewHtml" @click="onCopyClick"></div>
</template>

<style scoped lang="scss">
.md {
  min-width: 0;
  max-width: 100%;
  :deep(p) { margin: 0 0 0.7em; }
  :deep(p:last-child) { margin-bottom: 0; }
  :deep(ul), :deep(ol) { margin: 0 0 0.7em; padding-left: 1.4em; }
  :deep(li) { margin: 0.25em 0; }
  :deep(li > p) { margin: 0; }
  :deep(h1), :deep(h2), :deep(h3), :deep(h4), :deep(h5), :deep(h6) {
    margin: 0.9em 0 0.45em; line-height: 1.4; font-weight: 600; color: var(--ke-brand-700);
  }
  :deep(strong) { color: var(--ke-gray-900); font-weight: 600; }
  :deep(h1) { font-size: 1.3em; }
  :deep(h2) { font-size: 1.18em; }
  :deep(h3) { font-size: 1.08em; }
  :deep(h4), :deep(h5), :deep(h6) { font-size: 1em; }
  :deep(blockquote) {
    margin: 0.4em 0; padding: 6px 12px;
    border-left: 3px solid var(--ke-brand-200);
    background: var(--ke-brand-50); color: var(--ke-gray-600);
    border-radius: 0 var(--ke-radius-sm) var(--ke-radius-sm) 0;
  }
  :deep(code) {
    padding: 1px 5px; border-radius: 4px; font-size: 0.92em;
    background: var(--ke-brand-50); color: var(--ke-brand-700);
    font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  }
  :deep(mark) {
    background: var(--ke-brand-100); color: var(--ke-brand-800);
    padding: 0 2px; border-radius: 3px;
  }
  // 代码块：宽度锁定在正文列内，超长行只在本块内横向滚动（不撑宽对话框）
  :deep(pre) {
    position: relative; margin: 0.5em 0; padding: 10px 12px;
    max-width: 100%;
    background: #0f172a; color: #e2e8f0; border-radius: var(--ke-radius-sm);
    overflow-x: auto; white-space: pre; line-height: 1.6;
  }
  :deep(pre code) { padding: 0; background: transparent; color: inherit; }
  :deep(.md__pre) { position: relative; max-width: 100%; min-width: 0; }
  :deep(.md__copy) {
    position: absolute; top: 6px; right: 8px;
    border: 1px solid rgba(226, 232, 240, 0.3); background: rgba(226, 232, 240, 0.1);
    color: #cbd5e1; border-radius: 4px; padding: 1px 8px; font-size: 11px; cursor: pointer;
    opacity: 0; transition: opacity 0.15s;
  }
  :deep(.md__pre:hover .md__copy) { opacity: 1; }
  :deep(table) {
    border-collapse: collapse; margin: 0.5em 0; font-size: 0.95em;
    display: block; max-width: 100%; overflow-x: auto;   // 宽表格横向滚动
  }
  :deep(th), :deep(td) {
    border: 1px solid var(--ke-gray-200); padding: 4px 10px; text-align: left;
  }
  :deep(th) { background: var(--ke-gray-50); font-weight: 600; }
  :deep(a) { color: var(--ke-brand-600); text-decoration: none; }
  :deep(a:hover) { text-decoration: underline; }
  :deep(hr) { border: none; border-top: 1px solid var(--ke-gray-200); margin: 0.7em 0; }
  :deep(.katex-display) { margin: 0.5em 0; overflow-x: auto; overflow-y: hidden; }
  :deep(img) { max-width: 100%; }
  // Mermaid 图表：白底卡片 + 超宽图表横向滚动
  :deep(.md__mermaid) {
    margin: 0.6em 0; padding: 10px; background: var(--ke-diagram-bg);
    border: 1px solid var(--ke-gray-200); border-radius: var(--ke-radius-sm);
    overflow-x: auto; text-align: center;
  }
  :deep(.md__mermaid svg) { max-width: 100%; height: auto; }
  :deep(.md__mermaid-error) { color: var(--ke-danger); font-size: 12px; margin-bottom: 6px; text-align: left; }
}
</style>
