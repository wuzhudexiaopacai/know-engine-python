<script setup lang="ts">
// 应用根组件：路由出口 + 首屏顶部渐变加载条
import { onMounted, ref } from 'vue'

/** 首屏加载条：1.2s 跑完后淡出，仅展示一次 */
const loaderDone = ref(false)

onMounted(() => {
  window.setTimeout(() => {
    loaderDone.value = true
  }, 1200)
})
</script>

<template>
  <div class="app-loader" :class="{ 'is-done': loaderDone }" aria-hidden="true">
    <span />
  </div>
  <router-view />
</template>

<style scoped lang="scss">
.app-loader {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  z-index: 999;
  pointer-events: none;
  opacity: 1;
  transition: opacity 0.3s ease;

  &.is-done { opacity: 0; }

  span {
    display: block;
    height: 100%;
    width: 100%;
    transform-origin: left center;
    background: linear-gradient(
      90deg,
      var(--ke-brand-500),
      var(--ke-brand-400) 35%,
      var(--ke-brand-600) 65%,
      var(--ke-brand-500)
    );
    background-size: 200% 100%;
    animation: app-loader-run 1.2s var(--ke-ease-out) forwards,
               app-loader-flow 1.2s linear forwards;
  }
}

@keyframes app-loader-run {
  from { transform: scaleX(0); }
  to   { transform: scaleX(1); }
}
@keyframes app-loader-flow {
  from { background-position: 0 0; }
  to   { background-position: 200% 0; }
}
</style>
