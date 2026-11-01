<script setup lang="ts">
// 文件预览：Markdown/Word 渲染正文，Excel/CSV 渲染表格，PDF/图片走 iframe；
// 下载统一由顶部「下载」按钮触发（attachment），不再让浏览器预览时直接落盘。
import { computed, ref, watch } from 'vue'
import { Download, TopRight } from '@element-plus/icons-vue'
import { bundleUrl, downloadUrl, fetchPreviewContent, previewUrl } from '@/api/document'
import type { PreviewContent } from '@/api/document'
import MarkdownView from '@/components/common/MarkdownView.vue'

const props = defineProps<{
  docId: number | null
  versionId: number | null
  versionLabel?: string | null
}>()

const visible = defineModel<boolean>({ default: false })

type Kind = 'converted' | 'original'
const kind = ref<Kind>('converted')
const data = ref<PreviewContent | null>(null)
const loading = ref(false)
const error = ref('')
const activeSheet = ref(0)

const src = computed(() =>
  props.docId && props.versionId ? previewUrl(props.docId, props.versionId, kind.value) : '')
const downSrc = computed(() =>
  props.docId && props.versionId ? downloadUrl(props.docId, props.versionId, kind.value) : '')
const mode = computed(() => data.value?.mode ?? '')

async function load(): Promise<void> {
  data.value = null
  error.value = ''
  activeSheet.value = 0
  if (!visible.value || !props.docId || !props.versionId) return
  loading.value = true
  try {
    data.value = await fetchPreviewContent(props.docId, props.versionId, kind.value)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '预览加载失败'
  } finally {
    loading.value = false
  }
}

watch([visible, () => props.docId, () => props.versionId, kind],
  () => { if (visible.value) void load() })

/** 工作表：首行作表头，其余为数据行，列数按最长行对齐 */
const sheets = computed(() => data.value?.sheets ?? [])
const activeRows = computed(() => sheets.value[activeSheet.value]?.rows ?? [])
const sheetHeader = computed<string[]>(() => {
  const rows = activeRows.value
  const cols = rows.reduce((m, r) => Math.max(m, r.length), 0)
  const first = rows[0] ?? []
  return Array.from({ length: cols }, (_, i) => first[i] ?? '')
})
const sheetBody = computed(() => activeRows.value.slice(1))
const sheetTruncated = computed(() => !!sheets.value[activeSheet.value]?.truncated)

function downloadCurrent(): void {
  if (!props.docId || !props.versionId) return
  // Markdown 打包成 ZIP（md + images/），否则下载后的文件里图片链接指向服务器、看不到图
  const target = mode.value === 'markdown'
    ? bundleUrl(props.docId, props.versionId, kind.value)
    : downSrc.value
  if (target) window.open(target, '_blank', 'noopener')
}

function openExternal(): void {
  if (src.value) window.open(src.value, '_blank', 'noopener')
}
</script>

<template>
  <el-dialog v-model="visible" :title="`文件预览${versionLabel ? ` · v${versionLabel}` : ''}`"
             width="960px" top="6vh" append-to-body destroy-on-close class="pv__dialog">
    <div class="pv">
      <div class="pv__bar">
        <el-radio-group v-model="kind" size="small">
          <el-radio-button value="converted">转换结果</el-radio-button>
          <el-radio-button value="original">原始文件</el-radio-button>
        </el-radio-group>
        <div class="pv__bar-actions">
          <el-button size="small" type="primary" :icon="Download" @click="downloadCurrent">
            {{ mode === 'markdown' ? '下载 ZIP' : '下载' }}
          </el-button>
          <el-button v-if="mode === 'file'" size="small" :icon="TopRight" @click="openExternal">
            在新窗口打开
          </el-button>
        </div>
      </div>

      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />

      <div v-else v-loading="loading" class="pv__body">
        <div v-if="!loading && mode === 'markdown'" class="pv__md">
          <MarkdownView v-if="data?.text" :text="data.text" />
          <el-empty v-else description="暂无可预览内容" :image-size="80" />
        </div>

        <div v-else-if="!loading && mode === 'sheet'" class="pv__sheet">
          <el-tabs v-if="sheets.length > 1" v-model="activeSheet" class="pv__sheet-tabs">
            <el-tab-pane v-for="(s, i) in sheets" :key="i" :label="s.name || `Sheet${i + 1}`" :name="i" />
          </el-tabs>
          <el-alert v-if="sheetTruncated" type="info" :closable="false" show-icon
                    title="表格较大，预览仅展示前 200 行 / 30 列，完整内容请下载后查看" />
          <div class="pv__sheet-scroll">
            <table v-if="sheetHeader.length">
              <thead>
                <tr>
                  <th class="pv__idx">#</th>
                  <th v-for="i in sheetHeader.length" :key="i">{{ sheetHeader[i - 1] }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(r, ri) in sheetBody" :key="ri">
                  <td class="pv__idx">{{ ri + 1 }}</td>
                  <td v-for="i in sheetHeader.length" :key="i">{{ r[i - 1] }}</td>
                </tr>
              </tbody>
            </table>
            <el-empty v-else description="工作表为空" :image-size="80" />
          </div>
        </div>

        <div v-else-if="!loading && mode === 'file'" class="pv__frame">
          <iframe v-if="src" :src="src" title="文件预览" />
        </div>

        <el-empty v-else-if="!loading" description="该格式暂不支持在线预览，请点击上方「下载」查看"
                  :image-size="80" />
      </div>
    </div>
  </el-dialog>
</template>

<style scoped lang="scss">
.pv {
  &__bar {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: var(--ke-space-3);
  }
  &__bar-actions { display: flex; gap: var(--ke-space-2); }
  &__body { min-height: 320px; }
  &__md {
    max-height: 68vh;
    overflow: auto;
    padding: var(--ke-space-3) var(--ke-space-4);
    border: 1px solid var(--ke-gray-200);
    border-radius: var(--ke-radius-md);
    background: #fff;
  }
  &__sheet {
    display: flex; flex-direction: column; gap: var(--ke-space-2);
  }
  &__sheet-tabs { margin-bottom: 0; }
  &__sheet-scroll {
    max-height: 64vh;
    overflow: auto;
    border: 1px solid var(--ke-gray-200);
    border-radius: var(--ke-radius-md);
    background: #fff;
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      th, td {
        padding: 8px 12px;
        border-bottom: 1px solid var(--ke-gray-100);
        border-right: 1px solid var(--ke-gray-100);
        text-align: left;
        white-space: nowrap;
        max-width: 320px;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      th {
        position: sticky; top: 0; z-index: 1;
        background: #f3f8ff;
        color: var(--ke-brand-700);
        font-weight: 600;
      }
      tbody tr:hover td { background: #f7faff; }
      .pv__idx {
        width: 48px;
        color: var(--ke-gray-500);
        background: var(--ke-gray-25);
        text-align: center;
      }
    }
  }
  &__frame {
    height: 68vh;
    border: 1px solid var(--ke-gray-200);
    border-radius: var(--ke-radius-md);
    overflow: hidden;
    iframe { width: 100%; height: 100%; border: 0; }
  }
}
</style>
