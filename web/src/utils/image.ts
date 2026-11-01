/** 从消息正文提取 markdown 图片（![alt](url)），拆分出文字与图片地址 */

export interface MediaSplit {
  /** 去掉图片语法后的展示文字 */
  text: string
  images: string[]
}

// 兼容外链与站内相对地址（对话图片存 /api/files/...）
const MD_IMAGE = /!\[[^\]]*]\(([^)\s]+)(?:\s+"[^"]*")?\)/g

export function extractMdImages(content: string): MediaSplit {
  const images: string[] = []
  for (const m of content.matchAll(MD_IMAGE)) {
    images.push(m[1])
  }
  const text = content.replaceAll(MD_IMAGE, '').trim()
  return { text, images }
}
