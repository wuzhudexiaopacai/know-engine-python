<script setup lang="ts">
// 404 兜底页：给出去路不明的用户一条回家的路
import { useRouter } from 'vue-router'
const router = useRouter()
function goHome(): void {
  router.replace({ name: 'chat' })
}
</script>

<template>
  <div class="ke-404">
    <p class="ke-404__code">404</p>
    <h1 class="ke-404__title">页面走丢了</h1>
    <p class="ke-404__desc">您访问的页面不存在或已被移除</p>
    <el-button type="primary" @click="goHome">返回智能问答</el-button>
  </div>
</template>

<style scoped lang="scss">
.ke-404 {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--ke-space-2);
  background: var(--ke-gray-50);

  &__code {
    margin: 0;
    font-size: 72px;
    font-weight: 800;
    color: var(--ke-brand-100);
    line-height: 1;
  }
  &__title {
    margin: 0;
    font-size: 20px;
    color: var(--ke-gray-800);
  }
  &__desc {
    margin: 0 0 var(--ke-space-4);
    color: var(--ke-gray-500);
  }
}
</style>
