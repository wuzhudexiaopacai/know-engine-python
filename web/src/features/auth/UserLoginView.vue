<script setup lang="ts">
// 普通用户登录（手机号 + 口令 + 图形验证码）
import { ref, reactive, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { fetchCaptcha } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ tenant_code: '', phone: '', password: '', captcha_code: '' })

// 验证码：图片 + 一次性标识，提交失败后必须换新
const captchaImage = ref('')
const captchaId = ref('')
const captchaLoading = ref(false)

const rules: FormRules = {
  tenant_code: [
    { required: true, message: '请输入租户编码', trigger: 'blur' },
    { pattern: /^[a-z][a-z0-9_-]{2,31}$/, message: '租户编码为 3-32 位小写字母/数字/-/_', trigger: 'blur' },
  ],
  phone: [
    { required: true, message: '请输入手机号', trigger: 'blur' },
    { pattern: /^1\d{10}$/, message: '手机号格式不正确', trigger: 'blur' },
  ],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
  captcha_code: [{ required: true, message: '请输入验证码', trigger: 'blur' }],
}

async function refreshCaptcha(): Promise<void> {
  captchaLoading.value = true
  try {
    const data = await fetchCaptcha()
    captchaId.value = data.captcha_id
    captchaImage.value = data.image
    form.captcha_code = ''
  } catch {
    /* 拦截器已提示错误 */
  } finally {
    captchaLoading.value = false
  }
}

onMounted(refreshCaptcha)

async function submit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  if (!captchaId.value) {
    await refreshCaptcha()
    return
  }
  loading.value = true
  try {
    await auth.doLoginUser({
      tenant_code: form.tenant_code.trim(),
      phone: form.phone,
      password: form.password,
      captcha_id: captchaId.value,
      captcha_code: form.captcha_code,
    })
    ElMessage.success('登录成功')
    const redirect = route.query.redirect as string | undefined
    router.replace(redirect || { name: 'chat' })
  } catch {
    // 验证码一次性：换个新图，避免用户拿同一张图反复试
    await refreshCaptcha()
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth">
    <aside class="auth__hero">
      <div class="auth__hero-inner">
        <div class="auth__logo">K</div>
        <h1 class="auth__slogan">企业级智能知识引擎</h1>
        <p class="auth__sub">多源检索 · 流式问答 · 引用溯源 · 审核式问答缓存</p>
        <ul class="auth__points">
          <li><el-icon><Select /></el-icon> 向量 / 全文 / 混合检索与精排</li>
          <li><el-icon><Select /></el-icon> Text2SQL / Text2Cypher 结构化问答</li>
          <li><el-icon><Select /></el-icon> 全链路降级重试，稳定可用</li>
        </ul>
      </div>
    </aside>

    <main class="auth__panel">
      <div class="auth__card">
        <h2 class="auth__title">普通用户登录</h2>
        <p class="auth__desc">使用手机号登录，进行智能问答</p>

        <el-form ref="formRef" :model="form" :rules="rules" label-position="top"
                 class="auth__form" @submit.prevent="submit">
          <el-form-item label="租户编码" prop="tenant_code">
            <el-input v-model="form.tenant_code" placeholder="请输入租户编码，如 default"
                      size="large" clearable autocomplete="off" aria-label="租户编码">
              <template #prefix><el-icon><OfficeBuilding /></el-icon></template>
            </el-input>
          </el-form-item>
          <el-form-item label="手机号" prop="phone">
            <el-input v-model="form.phone" placeholder="请输入 11 位手机号" size="large"
                      inputmode="numeric" maxlength="11" clearable
                      aria-label="手机号">
              <template #prefix><el-icon><Iphone /></el-icon></template>
            </el-input>
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input v-model="form.password" type="password" placeholder="请输入密码"
                      size="large" show-password @keyup.enter="submit" aria-label="密码">
              <template #prefix><el-icon><Lock /></el-icon></template>
            </el-input>
          </el-form-item>
          <el-form-item label="验证码" prop="captcha_code">
            <div class="auth__captcha">
              <el-input v-model="form.captcha_code" placeholder="请输入验证码" size="large"
                        maxlength="4" autocomplete="off" aria-label="验证码">
                <template #prefix><el-icon><Key /></el-icon></template>
              </el-input>
              <button type="button" class="auth__captcha-img" :disabled="captchaLoading"
                      title="点击刷新验证码" @click="refreshCaptcha">
                <img v-if="captchaImage" :src="captchaImage" alt="验证码" />
                <span v-else class="auth__captcha-tip">加载中…</span>
              </button>
            </div>
          </el-form-item>
          <el-button type="primary" size="large" class="auth__submit" :loading="loading"
                     @click="submit">登录</el-button>
        </el-form>

        <div class="auth__foot">
          <router-link :to="{ name: 'admin-login' }" class="auth__switch">管理员登录 →</router-link>
        </div>
      </div>
    </main>
  </div>
</template>

<style scoped lang="scss">
.auth {
  display: grid;
  grid-template-columns: 1fr 1fr;
  height: 100vh;

  @media (max-width: 880px) {
    grid-template-columns: 1fr;
    .auth__hero { display: none; }
  }

  &__hero {
    position: relative;
    overflow: hidden;
    background: linear-gradient(160deg, var(--ke-login-hero-1), var(--ke-login-hero-2) 62%, var(--ke-login-hero-3));
    color: var(--ke-on-brand);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: var(--ke-space-8);
  }
  &__hero-inner { position: relative; z-index: 1; max-width: 420px; }
  &__logo {
    width: 48px; height: 48px; border-radius: 12px;
    background: rgba(255, 255, 255, 0.12);
    border: 1px solid rgba(255, 255, 255, 0.18);
    display: grid; place-items: center;
    font-size: 24px; font-weight: 700; margin-bottom: var(--ke-space-6);
  }
  &__slogan {
    font-size: 26px; font-weight: 600; margin: 0 0 var(--ke-space-3);
    letter-spacing: -0.01em;
    animation: auth-rise 0.5s var(--ke-ease-out) both;
  }
  &__sub {
    font-size: 14px; opacity: 0.78; margin: 0 0 var(--ke-space-8); line-height: 1.7;
    animation: auth-rise 0.5s var(--ke-ease-out) 0.12s both;
  }
  &__points {
    list-style: none; margin: 0; padding: 0;
    display: flex; flex-direction: column; gap: var(--ke-space-3);
    animation: auth-rise 0.5s var(--ke-ease-out) 0.24s both;

    li {
      display: flex; align-items: center; gap: var(--ke-space-3);
      font-size: 13px; opacity: 0.86;
    }
    .el-icon { color: var(--ke-login-hero-fg); }
  }

  &__panel {
    display: flex; align-items: center; justify-content: center;
    background: var(--ke-gray-25); padding: var(--ke-space-6);
  }
  &__card {
    width: 100%; max-width: 380px;
    background: var(--ke-bg-card); border: 1px solid var(--ke-gray-200);
    border-radius: var(--ke-radius-lg); box-shadow: var(--ke-shadow-sm);
    padding: var(--ke-space-8);
  }
  &__title {
    margin: 0 0 4px; font-size: 20px; font-weight: 600; color: var(--ke-gray-900);
    animation: auth-rise 0.5s var(--ke-ease-out) both;
  }
  &__desc {
    margin: 0 0 var(--ke-space-6); font-size: 13px; color: var(--ke-gray-500);
    animation: auth-rise 0.5s var(--ke-ease-out) 0.08s both;
  }
  &__form { animation: auth-rise 0.5s var(--ke-ease-out) 0.16s both; }
  &__captcha {
    display: flex; align-items: center; gap: var(--ke-space-3); width: 100%;
  }
  &__captcha-img {
    flex: none; width: 132px; height: 40px; padding: 0;
    border: 1px solid var(--ke-gray-200); border-radius: var(--ke-radius-sm);
    background: var(--ke-gray-50); cursor: pointer; overflow: hidden;
    display: grid; place-items: center;
    transition: border-color var(--ke-duration-fast);

    &:hover { border-color: var(--ke-brand-500); }
    &:disabled { cursor: progress; opacity: 0.7; }
    img { width: 100%; height: 100%; object-fit: cover; display: block; }
  }
  &__captcha-tip { font-size: 12px; color: var(--ke-gray-400); }
  &__submit {
    width: 100%; margin-top: var(--ke-space-2);
    transition: box-shadow var(--ke-duration-fast), transform var(--ke-duration-fast);
    &:hover { box-shadow: var(--ke-shadow-brand); }
    &:active { transform: scale(0.98); }
  }
  &__foot {
    display: flex; align-items: center; justify-content: center;
    margin-top: var(--ke-space-5); font-size: 13px;
    animation: auth-rise 0.5s var(--ke-ease-out) 0.24s both;
  }
  &__switch { color: var(--ke-brand-500); font-weight: 500; }

  /* ---------- H5：单栏铺满，卡片内边距收紧，适配软键盘与安全区 ---------- */
  @media (max-width: 480px) {
    height: auto;
    min-height: 100vh;
    min-height: 100dvh;

    &__panel {
      align-items: flex-start;
      min-height: 100vh;
      min-height: 100dvh;
      padding: max(24px, 8vh) 16px calc(24px + env(safe-area-inset-bottom));
    }
    &__card { padding: var(--ke-space-6) var(--ke-space-5); }
    &__captcha-img { width: 108px; }
  }
}

// 登录文字分组依次淡入上浮，ease-out 收尾
@keyframes auth-rise {
  from { opacity: 0; transform: translateY(10px); }
  to   { opacity: 1; transform: translateY(0); }
}
</style>
