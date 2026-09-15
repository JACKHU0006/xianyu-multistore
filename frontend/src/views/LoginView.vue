<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'

const router = useRouter()
const session = useSessionStore()

const username = ref('')
const password = ref('')
const tenantId = ref('')
const error = ref('')
const busy = ref(false)

async function submit() {
  error.value = ''
  busy.value = true
  try {
    await session.login(username.value.trim(), password.value, tenantId.value.trim())
    router.push('/')
  } catch (err) {
    error.value = err?.message || '登录失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <form class="login-card" @submit.prevent="submit">
      <h1>登录 · 多店铺智控台</h1>
      <p class="hint">用店铺账号登录，凭证是服务端签发的 JWT。</p>

      <label>
        <span>用户名</span>
        <input v-model="username" autocomplete="username" placeholder="owner" required />
      </label>

      <label>
        <span>密码</span>
        <input v-model="password" type="password" autocomplete="current-password"
               placeholder="••••••••" required />
      </label>

      <label>
        <span>租户 ID（可选）</span>
        <input v-model="tenantId" placeholder="多租户重名时才需要填" />
      </label>

      <p v-if="error" class="err">{{ error }}</p>

      <button type="submit" :disabled="busy">{{ busy ? '登录中…' : '登录' }}</button>

      <p class="tip">演示账号：owner / demo1234（播种数据默认创建）</p>
    </form>
  </div>
</template>

<style scoped>
.login-wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.login-card {
  width: 100%;
  max-width: 360px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 28px;
  border-radius: var(--border-radius-lg, 12px);
  background: var(--color-background-primary, #fff);
  border: 0.5px solid var(--color-border-tertiary, rgba(0, 0, 0, 0.15));
}
h1 {
  margin: 0;
  font-size: 15px;
  font-weight: 500;
}
.hint,
.tip {
  margin: 0;
  font-size: 12px;
  color: var(--color-text-secondary, #666);
}
.tip {
  text-align: center;
  color: var(--color-text-tertiary, #999);
}
label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}
input {
  padding: 9px 11px;
  font-size: 13px;
  border-radius: 8px;
  border: 0.5px solid var(--color-border-secondary, rgba(0, 0, 0, 0.3));
  background: var(--color-background-secondary, #fafafa);
  color: inherit;
}
button {
  padding: 10px;
  font-size: 13px;
  font-weight: 500;
  border-radius: 8px;
  border: none;
  cursor: pointer;
  background: #185fa5;
  color: #fff;
}
button:disabled {
  opacity: 0.6;
  cursor: default;
}
.err {
  margin: 0;
  font-size: 12px;
  color: #a32d2d;
}
</style>
