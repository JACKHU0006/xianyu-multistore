<script setup>
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { navRoutes } from '@/router'
import { useSessionStore } from '@/stores/session'
import StatusPill from '@/components/StatusPill.vue'

const session = useSessionStore()
const route = useRoute()
const router = useRouter()

onMounted(() => {
  // 登录页不需要预取身份
  if (route.name !== 'login') session.bootstrap()
})

const roleLabel = { OWNER: '店主', MANAGER: '运营主管', AGENT: '客服', VIEWER: '只读' }
const scopeLabel = computed(() =>
  session.principal?.tenant_wide
    ? '全部店铺'
    : `${session.stores.length} 家店铺`,
)

function doLogout() {
  session.logout()
  router.push('/login')
}
</script>

<template>
  <router-view v-if="route.name === 'login'" />

  <div v-else class="app">
    <aside class="sidebar">
      <div class="brand">
        <div class="logo">闲</div>
        <div>
          <h1>多店铺智控台</h1>
          <p>MULTI-STORE CONSOLE</p>
        </div>
      </div>

      <div>
        <div class="sec-label">我的店铺</div>
        <div v-if="session.stores.length">
          <div
            v-for="store in session.stores"
            :key="store.id"
            class="store-item"
            :class="{ active: store.id === session.currentStoreId }"
            @click="session.selectStore(store.id)"
          >
            <span class="dot" :class="store.status === 'ONLINE' ? 'ok' : store.status === 'NEEDS_HUMAN' ? 'warn' : 'idle'" />
            <span class="nm">
              <b>{{ store.name }}</b>
              <span>{{ store.account }} · {{ store.owner }}</span>
            </span>
          </div>
        </div>
        <div v-else class="hint" style="padding: 0 8px">
          {{ session.ready ? '当前身份没有可见店铺' : '加载中…' }}
        </div>
      </div>

      <nav class="nav">
        <router-link
          v-for="item in navRoutes"
          :key="item.name"
          :to="item.path"
          :class="{ active: route.name === item.name }"
        >
          <span class="ic">{{ item.meta.icon }}</span>
          <span>{{ item.meta.title }}</span>
        </router-link>
      </nav>

      <div class="compliance">
        <b>合规模式已开启</b>
        本控制台不提供 Cookie 共享、设备伪装、代理轮换与验证码绕过。人机验证一律转人工，由店主本人处理。
      </div>
    </aside>

    <main class="main">
      <header class="topbar">
        <div>
          <h2>{{ route.meta?.title || '总览' }}</h2>
          <div class="sub">
            <template v-if="session.principal">
              {{ session.principal.user_id }} ·
              {{ roleLabel[session.principal.role] || session.principal.role }} ·
              {{ scopeLabel }}
            </template>
            <template v-else>未连接</template>
          </div>
        </div>
        <div class="spacer" />
        <StatusPill v-if="session.error" tone="danger">{{ session.error }}</StatusPill>
        <StatusPill v-else-if="!session.ready" tone="idle">连接中…</StatusPill>
        <StatusPill v-else tone="ok">已连接</StatusPill>
        <button v-if="session.token" class="auth-btn" @click="doLogout">退出登录</button>
        <router-link v-else class="auth-btn" to="/login">登录</router-link>
      </header>

      <div class="view">
        <router-view v-if="session.ready && !session.error" />
        <div v-else-if="session.error" class="banner danger">
          <div>
            <b>无法连接后端</b>
            <p>{{ session.error }}。请确认后端已启动，且 vite 代理指向正确（默认 127.0.0.1:8000）。</p>
          </div>
        </div>
      </div>
    </main>

    <div v-if="session.toast" class="toast" :class="{ error: session.toast.isError }">
      {{ session.toast.message }}
    </div>
  </div>
</template>

<style scoped>
.auth-btn {
  margin-left: 8px;
  padding: 5px 12px;
  font-size: 12px;
  border-radius: 8px;
  border: 0.5px solid var(--color-border-secondary, rgba(0, 0, 0, 0.3));
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-decoration: none;
}
.auth-btn:hover {
  border-color: var(--color-border-primary, rgba(0, 0, 0, 0.4));
}
</style>
