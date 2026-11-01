<script setup lang="ts">
// 模型配置：管理员维护本地/云端对话模型，用户在对话页可选择并看到模型名称
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  bindModelRole, createModel, deleteModel, listModelRoles, listModels,
  testModel, updateModel,
} from '@/api/admin'
import type { ModelRoleVO, ModelSavePayload, ModelVO, ModelTestPayload } from '@/types/admin'
import { formatDateTime } from '@/utils/format'

/** 供应商枚举与展示名（全部走 OpenAI 兼容协议）；short 用于表格内的紧凑标签 */
interface ProviderOption {
  value: string
  label: string
  short?: string
  hint: string
}
const PROVIDERS: ProviderOption[] = [
  { value: 'aliyun', label: '阿里云', hint: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  { value: 'bailian', label: '阿里百炼', hint: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  { value: 'zhipu', label: '智谱 GLM', hint: 'https://open.bigmodel.cn/api/paas/v4' },
  { value: 'openai', label: 'OpenAI', hint: 'https://api.openai.com/v1' },
  { value: 'local', label: '本地部署（Ollama/vLLM）', short: '本地部署', hint: 'http://127.0.0.1:11434/v1（地址到 /v1 为止，无需密钥；CPU 推理建议 3B~7B 量化模型）' },
  { value: 'custom', label: '自定义', hint: '填 OpenAI 兼容接口地址，一般以 /v1 结尾' },
]
const providerLabel = (v: string): string => PROVIDERS.find((p) => p.value === v)?.label ?? v
const providerShort = (v: string): string => {
  const provider = PROVIDERS.find((p) => p.value === v)
  return provider?.short ?? provider?.label ?? v
}

/** 各供应商的默认回填值：新增时自动填入，用户可自由修改，也可一键恢复 */
interface ProviderDefaults {
  display_name: string
  model_name: string
  base_url: string
}
const PROVIDER_DEFAULTS: Record<string, ProviderDefaults> = {
  aliyun: { display_name: '通义千问 Plus', model_name: 'qwen-plus',
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  bailian: { display_name: '百炼 Qwen Plus', model_name: 'qwen-plus',
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  zhipu: { display_name: '智谱 GLM-4', model_name: 'glm-4-air',
    base_url: 'https://open.bigmodel.cn/api/paas/v4' },
  openai: { display_name: 'OpenAI GPT-4o mini', model_name: 'gpt-4o-mini',
    base_url: 'https://api.openai.com/v1' },
  local: { display_name: '本地 Qwen2.5 3B', model_name: 'qwen2.5:3b-instruct',
    base_url: 'http://127.0.0.1:11434/v1' },
  custom: { display_name: '', model_name: '', base_url: '' },
}

/** 按当前供应商回填展示名、模型名、接口地址（不改密钥/开关等用户设置） */
function applyProviderDefaults(): void {
  const d = PROVIDER_DEFAULTS[form.provider] ?? PROVIDER_DEFAULTS.custom
  form.display_name = d.display_name
  form.model_name = d.model_name
  form.base_url = d.base_url
}

/** 切换供应商：新增时自动回填；编辑时保留已填内容，交由用户点“回填默认值” */
function onProviderChange(value: string): void {
  form.provider = value
  if (!editingId.value) applyProviderDefaults()
}

const rows = ref<ModelVO[]>([])
const loading = ref(false)
/** 模型用途（系统各位置当前绑定哪个模型） */
const roles = ref<ModelRoleVO[]>([])
const rolesLoading = ref(false)
const bindingRole = ref<string | null>(null)
const dialogOpen = ref(false)
const saving = ref(false)
const testing = ref(false)
/** 编辑中的模型 id；null 表示新增 */
const editingId = ref<number | null>(null)
const lockVersion = ref(0)

const PURPOSES = [
  { value: 'chat', label: '对话模型', hint: '意图识别、改写与回答生成' },
  { value: 'vision', label: '图片理解', hint: '文档图片描述 / 对话看图，需支持图片输入' },
  { value: 'speech', label: '语音识别', hint: '录音转文字（ASR）' },
  { value: 'embedding', label: '文本向量化', hint: '入库与检索的 embedding 模型' },
  { value: 'rerank', label: '检索精排', hint: '检索结果相关性精排' },
  { value: 'image_gen', label: '文生图', hint: '对话页生成图片' },
]
const purposeLabel = (v: string): string => PURPOSES.find((p) => p.value === v)?.label ?? v
const purposeTagType = (v: string): 'primary' | 'success' | 'warning' | 'info' | 'danger' => ({
  chat: 'primary', vision: 'warning', speech: 'success',
  embedding: 'info', rerank: 'danger', image_gen: 'success',
} as const)[v] ?? 'info'

const form = reactive<ModelSavePayload>({
  purpose: ['chat'],
  provider: 'aliyun',
  display_name: '',
  model_name: '',
  base_url: '',
  api_key: '',
  is_default: false,
  enabled: true,
  sort: 0,
  remark: '',
  supports_vision: false,
})

const dialogTitle = computed(() => (editingId.value ? '编辑模型配置' : '新增模型配置'))
const providerHint = computed(
  () => PROVIDERS.find((p) => p.value === form.provider)?.hint ?? '',
)

async function load(): Promise<void> {
  loading.value = true
  rolesLoading.value = true
  try {
    const [models, roleList] = await Promise.all([listModels(), listModelRoles()])
    rows.value = models
    roles.value = roleList
  } catch {
    /* 拦截器已提示 */
  } finally {
    loading.value = false
    rolesLoading.value = false
  }
}

/** 用途绑定：选空值表示解绑，回退默认模型/环境变量 */
async function onBindRole(role: ModelRoleVO, modelId: number | null | undefined): Promise<void> {
  bindingRole.value = role.role
  try {
    roles.value = await bindModelRole(role.role, modelId ?? null)
    ElMessage.success(`${role.label}已更新`)
  } catch {
    /* 拦截器已提示 */
  } finally {
    bindingRole.value = null
  }
}

function resetForm(): void {
  form.purpose = ['chat']
  form.provider = 'aliyun'
  form.display_name = ''
  form.model_name = ''
  form.base_url = ''
  form.api_key = ''
  form.is_default = false
  form.enabled = true
  form.sort = 0
  form.remark = ''
  form.supports_vision = false
}

function openCreate(): void {
  editingId.value = null
  lockVersion.value = 0
  resetForm()
  applyProviderDefaults()
  dialogOpen.value = true
}

function openEdit(row: ModelVO): void {
  editingId.value = row.id
  lockVersion.value = row.lock_version
  form.purpose = (row.purpose || 'chat').split(',').map((s) => s.trim()).filter(Boolean)
  form.provider = row.provider
  form.display_name = row.display_name
  form.model_name = row.model_name
  form.base_url = row.base_url
  form.api_key = ''
  form.is_default = row.is_default === 1
  form.enabled = row.enabled === 1
  form.sort = row.sort
  form.remark = row.remark ?? ''
  form.supports_vision = row.supports_vision === 1
  dialogOpen.value = true
}

/** 表单校验：必填与地址格式（密钥编辑时可留空沿用旧值） */
function validateForm(): boolean {
  if (!form.display_name.trim()) { ElMessage.warning('请填写展示名称'); return false }
  if (!form.model_name.trim()) { ElMessage.warning('请填写上游模型名'); return false }
  if (!form.base_url.trim()) { ElMessage.warning('请填写接口地址'); return false }
  return true
}

async function save(): Promise<void> {
  if (!validateForm() || saving.value) return
  saving.value = true
  const payload: ModelSavePayload = {
    ...form,
    display_name: form.display_name.trim(),
    model_name: form.model_name.trim(),
    base_url: form.base_url.trim(),
    api_key: form.api_key.trim(),
    purpose: (form.purpose as string[]).join(','),
    remark: form.remark?.trim() || null,
  }
  try {
    if (editingId.value) {
      await updateModel(editingId.value, payload, lockVersion.value)
      ElMessage.success('模型已更新')
    } else {
      await createModel(payload)
      ElMessage.success('模型已新增')
    }
    dialogOpen.value = false
    await load()
  } catch {
    /* 拦截器已提示 */
  } finally {
    saving.value = false
  }
}

async function remove(row: ModelVO): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定删除模型「${row.display_name}」吗？删除后用户将不再看到该模型。`,
      '删除模型', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch { return }
  try {
    await deleteModel(row.id, row.lock_version)
    ElMessage.success('模型已删除')
    await load()
  } catch { /* 拦截器已提示 */ }
}

/** 连通性测试：优先测当前表单；编辑态未填密钥时后端自动取库内密钥 */
async function runTest(): Promise<void> {
  if (!form.base_url.trim() || !form.model_name.trim()) {
    ElMessage.warning('请先填写接口地址与模型名')
    return
  }
  testing.value = true
  try {
    const payload: ModelTestPayload = {
      id: editingId.value,
      base_url: form.base_url.trim(),
      model_name: form.model_name.trim(),
      api_key: form.api_key.trim(),
      purpose: (form.purpose as string[]).join(','),
    }
    const res = await testModel(payload)
    if (res.ok) {
      ElMessage.success(`连通正常（${res.latency_ms} ms）`)
    } else {
      ElMessage.error(`连通失败（${res.latency_ms} ms）：${res.message}`)
    }
  } catch {
    /* 拦截器已提示 */
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="ke-page model">
    <header class="model__head">
      <div>
        <h1 class="model__title">模型配置</h1>
        <p class="model__sub ke-muted">先在此录入可用的模型（对话 / 图片理解 / 语音识别 / 向量化 / 精排 / 文生图），再到下方「模型用途」为系统每个位置指定使用哪个模型</p>
      </div>
      <el-button type="primary" :icon="'Plus'" @click="openCreate">新增模型</el-button>
    </header>

    <section class="model__roles ke-card" aria-label="模型用途">
      <div class="model__roles-head">
        <div>
          <h2 class="model__roles-title">模型用途</h2>
          <p class="model__roles-sub ke-muted">每个用途可从未绑定的已启用模型中选择；留空表示回退到该用途的默认模型或环境变量配置</p>
        </div>
      </div>
      <div v-loading="rolesLoading" class="model__roles-list">
        <div v-for="role in roles" :key="role.role" class="model__role">
          <div class="model__role-info">
            <div class="model__role-name">
              {{ role.label }}
              <el-tag size="small" type="info" effect="plain">{{ purposeLabel(role.capability) }}</el-tag>
            </div>
            <div class="model__role-desc ke-muted">{{ role.description }}</div>
          </div>
          <div class="model__role-select">
            <el-select
              :model-value="role.model ? role.model.id : null"
              :loading="bindingRole === role.role"
              clearable
              placeholder="未绑定（使用默认/环境变量）"
              class="model__role-selector"
              @change="(v: number | null | undefined) => onBindRole(role, v)"
            >
              <el-option
                v-for="m in role.candidates"
                :key="m.id"
                :label="`${m.display_name}（${m.model_name}）`"
                :value="m.id"
              />
            </el-select>
            <div v-if="!role.model && role.fallback" class="model__role-fallback ke-muted">
              当前回退：{{ role.fallback }}
            </div>
            <div v-else-if="!role.model" class="model__role-fallback model__warn">
              当前不可用：请新增「{{ purposeLabel(role.capability) }}」用途的模型并绑定
            </div>
          </div>
        </div>
        <el-empty v-if="!roles.length" description="暂无用途数据" :image-size="60" />
      </div>
    </section>

    <section class="model__table ke-card ke-card--table" aria-label="模型列表">
      <el-table v-loading="loading" :data="rows" stripe row-key="id">
        <template #empty>
          <el-empty description="尚未配置模型，点击右上角新增" />
        </template>

        <el-table-column label="展示名称" min-width="240">
          <template #default="{ row }">
            <div class="model__cell">
              <span class="model__name ke-truncate">{{ row.display_name }}</span>
              <span class="model__remark ke-truncate">{{ row.remark || row.model_name }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="用途" min-width="160">
          <template #default="{ row }">
            <el-tag
              v-for="p in (row.purpose || 'chat').split(',')"
              :key="p"
              size="small"
              effect="light"
              :type="purposeTagType(p.trim())"
              style="margin-right: 4px; margin-bottom: 2px"
            >
              {{ purposeLabel(p.trim()) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="供应商" width="140">
          <template #default="{ row }">
            <el-tooltip :content="providerLabel(row.provider)" placement="top" :show-after="400">
              <el-tag size="small" type="info" effect="plain">{{ providerShort(row.provider) }}</el-tag>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="上游模型名" min-width="190">
          <template #default="{ row }">
            <span class="model__upstream">{{ row.model_name }}</span>
          </template>
        </el-table-column>

        <el-table-column label="接口地址" min-width="220">
          <template #default="{ row }">
            <el-tooltip :content="row.base_url" placement="top" :show-after="400">
              <span class="model__url ke-truncate">{{ row.base_url }}</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="密钥" width="150">
          <template #default="{ row }">
            <el-tooltip v-if="row.has_api_key" :content="row.api_key_masked" placement="top" :show-after="400">
              <span class="model__key ke-truncate ke-muted">{{ row.api_key_masked }}</span>
            </el-tooltip>
            <span v-else class="model__warn">未配置</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="150">
          <template #default="{ row }">
            <div class="model__tags">
              <el-tag v-if="row.is_default === 1" size="small" type="primary" effect="light">默认</el-tag>
              <el-tag v-if="row.enabled === 0" size="small" type="info" effect="light">已停用</el-tag>
              <el-tag v-else size="small" type="success" effect="light">已启用</el-tag>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="更新时间" width="150">
          <template #default="{ row }"><span class="ke-muted">{{ formatDateTime(row.updated_at) }}</span></template>
        </el-table-column>

        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <div class="ke-actions">
              <el-button link type="primary" size="small" @click="openEdit(row as ModelVO)">编辑</el-button>
              <el-button link type="danger" size="small" @click="remove(row as ModelVO)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <el-dialog v-model="dialogOpen" :title="dialogTitle" width="min(640px, 94vw)" append-to-body>
      <el-form :model="form" label-position="top">
        <el-form-item label="用途（可多选）">
          <el-checkbox-group v-model="(form.purpose as string[])" class="model__purpose-group">
            <el-checkbox
              v-for="p in PURPOSES"
              :key="p.value"
              :value="p.value"
            >
              <span class="model__purpose-label">{{ p.label }}</span>
              <span class="model__purpose-hint">{{ p.hint }}</span>
            </el-checkbox>
          </el-checkbox-group>
          <p class="model__tip">
            一个模型可同时勾选多种能力（如 qwen3.6 可同时做对话、图片理解、文生图）；
            专用模型（如 embedding / rerank）通常只勾选一项。
          </p>
          <p v-if="form.purpose.includes('vision')" class="model__tip">
            勾选「图片理解」时，该模型必须支持图片输入（如 qwen3.6-vl / glm-4v / llava）。
          </p>
        </el-form-item>
        <el-form-item label="供应商">
          <el-select v-model="form.provider" class="model__full" @change="onProviderChange">
            <el-option v-for="p in PROVIDERS" :key="p.value" :label="p.label" :value="p.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="展示名称" required>
          <el-input v-model="form.display_name" maxlength="64" placeholder="用户在对话页看到的名字，如：通义千问 Plus" />
        </el-form-item>
        <el-form-item label="上游模型名" required>
          <el-input v-model="form.model_name" maxlength="128" placeholder="如：qwen-plus / glm-4 / gpt-4o-mini" />
        </el-form-item>
        <el-form-item label="接口地址（OpenAI 兼容）" required>
          <el-input v-model="form.base_url" maxlength="512" :placeholder="providerHint" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            maxlength="512"
            :placeholder="editingId ? '留空表示沿用原密钥' : '本地部署可留空'"
          />
        </el-form-item>
        <div class="model__row">
          <el-form-item label="设为默认">
            <el-switch v-model="form.is_default" />
          </el-form-item>
          <el-form-item label="启用">
            <el-switch v-model="form.enabled" />
          </el-form-item>
          <el-form-item label="排序（越小越靠前）">
            <el-input-number v-model="form.sort" :min="0" :max="999" controls-position="right" />
          </el-form-item>
        </div>
        <el-form-item label="备注">
          <el-input v-model="form.remark" maxlength="512" placeholder="可选，仅后台可见" />
        </el-form-item>
        <el-form-item label="支持图片输入">
          <el-switch v-model="form.supports_vision" />
          <span class="model__tip" style="margin-left: 12px">
            开启后，RAG 检索到含图片的文档时会把图片发给该模型阅读；不开启则只传文字描述，避免模型不支持 image_url 报错。
          </span>
        </el-form-item>
        <div class="model__test">
          <el-button :loading="testing" @click="runTest">测试连通性</el-button>
          <el-button @click="applyProviderDefaults">回填默认值</el-button>
          <span class="ke-muted">发送一条最小补全请求，验证地址、密钥与模型名</span>
        </div>
      </el-form>
      <template #footer>
        <el-button :disabled="saving" @click="dialogOpen = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.model {
  &__head { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: var(--ke-space-5); }
  &__title { margin: 0; font-size: 20px; font-weight: 600; letter-spacing: -0.01em; color: var(--ke-gray-900); }
  &__sub { margin: 4px 0 0; font-size: 13px; max-width: 620px; }
  &__tags { display: flex; align-items: center; gap: 6px; }

  &__cell { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  &__name, &__remark { display: block; max-width: 100%; }
  &__name { color: var(--ke-gray-800); font-weight: 500; }
  &__remark { font-size: 12px; color: var(--ke-gray-400); }
  &__upstream { display: block; white-space: nowrap; color: var(--ke-gray-600); }
  &__url { display: block; max-width: 100%; font-size: 12px; color: var(--ke-gray-600); }
  &__key { display: block; max-width: 100%; font-size: 12px; }
  &__warn { color: var(--ke-danger); }

  &__tip { margin: 6px 0 0; font-size: 12px; line-height: 1.6; color: var(--ke-gray-500); }
  &__full { width: 100%; }
  &__row { display: flex; align-items: flex-end; gap: var(--ke-space-6); flex-wrap: wrap; }
  &__test { display: flex; align-items: center; gap: var(--ke-space-3); }

  &__purpose-group {
    display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px 16px;
  }
  &__purpose-label { font-weight: 500; color: var(--ke-gray-800); }
  &__purpose-hint {
    display: block; font-size: 11px; color: var(--ke-gray-400); margin-top: 2px;
  }

  &__roles { margin-bottom: var(--ke-space-5); padding: var(--ke-space-5); }
  &__roles-head { margin-bottom: var(--ke-space-4); }
  &__roles-title { margin: 0; font-size: 15px; font-weight: 600; color: var(--ke-gray-900); }
  &__roles-sub { margin: 4px 0 0; font-size: 12px; }
  &__roles-list { display: flex; flex-direction: column; }
  &__role {
    display: flex; align-items: flex-start; justify-content: space-between;
    gap: var(--ke-space-5); padding: var(--ke-space-4) 0;
    border-top: 1px solid var(--ke-gray-100);
    &:first-child { border-top: 0; }
  }
  &__role-info { flex: 1; min-width: 0; }
  &__role-name { display: flex; align-items: center; gap: 8px; font-weight: 500; color: var(--ke-gray-800); }
  &__role-desc { margin-top: 4px; font-size: 12px; line-height: 1.6; }
  &__role-select { width: 320px; flex: none; }
  &__role-selector { width: 100%; }
  &__role-fallback { margin-top: 4px; font-size: 12px; }
}
</style>
