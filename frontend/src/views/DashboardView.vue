<script setup>
import { computed, ref, watch } from 'vue'
import { api } from '@/api'
import { useSessionStore } from '@/stores/session'
import StatCard from '@/components/StatCard.vue'
import StatusPill from '@/components/StatusPill.vue'

const session = useSessionStore()
const health = ref(null)
const metrics = ref(null)
const loading = ref(false)

const scoreTone = computed(() => {
  const s = health.value?.score ?? 100
  return s >= 80 ? 'ok' : s >= 60 ? 'warn' : 'danger'
})

const usage = computed(() => metrics.value?.usage || {})
const retries = computed(() => metrics.value?.retries || {})
const handoff = computed(() => metrics.value?.handoff || {})
const dedup = computed(() => health.value?.dedup || {})

const dedupTone = computed(() => {
  const level = dedup.value.level
  return level === 'ALARMING' ? 'danger' : level === 'ELEVATED' ? 'warn' : 'ok'
})

const statusTone = (s) => (s === 'ONLINE' ? 'ok' : s === 'NEEDS_HUMAN' ? 'warn' : 'idle')
const statusText = (s) =>
  ({ ONLINE: '在线', NEEDS_HUMAN: '待人工验证', OFFLINE: '已停用' }[s] || s)

async function load() {
  if (!session.currentStoreId) return
  loading.value = true
  try {
    const [h, m] = await Promise.all([
      api.analyticsHealth(session.currentStoreId),
      api.metrics(session.currentStoreId),
    ])
    health.value = h
    metrics.value = m
  } catch (err) {
    session.notify(err?.message || String(err), true)
  } finally {
    loading.value = false
  }
}

watch(() => session.currentStoreId, load, { immediate: true })
</script>

<template>
  <div class="stats">
    <StatCard
      label="健康分"
      :value="health ? `${health.score}/100` : '—'"
      :tone="scoreTone"
      :detail="health?.issues?.length ? `${health.issues.length} 项待处理` : '各项正常'"
    />
    <StatCard
      label="今日咨询"
      :value="usage.turns ?? '—'"
      :detail="`AI 自动处理 ${usage.llm_calls ?? 0} 次 / FAQ 命中 ${usage.faq_hits ?? 0} 次`"
    />
    <StatCard
      label="FAQ 命中率"
      :value="usage.faq_hit_rate != null ? `${(usage.faq_hit_rate * 100).toFixed(1)}%` : '—'"
      :tone="(usage.faq_hit_rate ?? 0) >= 0.6 ? 'ok' : 'warn'"
      detail="命中越高，响应越快、成本越低"
    />
    <StatCard
      label="待处理工单"
      :value="handoff.open ?? '—'"
      :tone="handoff.needs_attention ? 'danger' : 'ok'"
      :detail="handoff.breached ? `已超时 ${handoff.breached} 单` : '均未超时'"
    />
  </div>

  <div v-if="health && health.issues.length" class="banner">
    <div>
      <b>健康检查发现 {{ health.issues.length }} 项不达标</b>
      <p v-for="issue in health.issues" :key="issue">· {{ issue }}</p>
    </div>
  </div>

  <div class="card">
    <div class="card-h">
      <h3>重复消息率</h3>
      <span class="desc">这个指标同时是免费的断线探测器 —— 突然升高说明平台在重投</span>
      <div class="spacer" />
      <StatusPill :tone="dedupTone">{{ dedup.level || '—' }}</StatusPill>
    </div>
    <div class="card-b">
      <div class="kv">
        <span class="k">去重总量</span>
        <span class="v">{{ dedup.total ?? 0 }} 条，其中重复 {{ dedup.duplicates ?? 0 }} 条</span>
      </div>
      <div class="kv">
        <span class="k">重复率</span>
        <span class="v">{{ dedup.rate != null ? `${(dedup.rate * 100).toFixed(1)}%` : '—' }}</span>
      </div>
      <div class="kv">
        <span class="k">判断</span>
        <span class="v muted" style="max-width: 60%; font-weight: 400">{{ dedup.diagnosis || '—' }}</span>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-h">
      <h3>店铺矩阵</h3>
      <span class="desc">各店铺状态（数据相互隔离）</span>
      <div class="spacer" />
      <button class="btn sm" :disabled="loading" @click="load">刷新</button>
    </div>
    <div class="card-b" style="padding-top: 4px">
      <table v-if="session.stores.length">
        <thead>
          <tr>
            <th>店铺</th>
            <th>负责人</th>
            <th>状态</th>
            <th>自动回复</th>
            <th>自动发货</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="store in session.stores"
            :key="store.id"
            style="cursor: pointer"
            @click="session.selectStore(store.id)"
          >
            <td>
              <div class="t1">{{ store.name }}</div>
              <div class="t2 mono">{{ store.account }}</div>
            </td>
            <td>{{ store.owner }}</td>
            <td><StatusPill :tone="statusTone(store.status)">{{ statusText(store.status) }}</StatusPill></td>
            <td>{{ store.auto_reply ? '开' : '关' }}</td>
            <td>{{ store.auto_ship ? '开' : '关' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">当前身份没有可见店铺</div>
    </div>
  </div>

  <div class="card">
    <div class="card-h">
      <h3>运行指标</h3>
      <span class="desc">补偿队列、限流器与成本</span>
    </div>
    <div class="card-b">
      <div class="kv">
        <span class="k">补偿队列</span>
        <span class="v">
          待处理 {{ retries.pending ?? 0 }} · 死信 {{ retries.dead ?? 0 }}
        </span>
      </div>
      <div class="kv">
        <span class="k">AI 成本</span>
        <span class="v">
          ¥{{ (usage.cost ?? 0).toFixed(4) }}（单轮 ¥{{ (usage.avg_cost_per_turn ?? 0).toFixed(6) }}）
        </span>
      </div>
      <div class="kv">
        <span class="k">省下的模型调用</span>
        <span class="v">{{ usage.saved_calls ?? 0 }} 次（FAQ 命中）</span>
      </div>
      <div v-if="retries.needs_attention" class="hint" style="margin-top: 10px">
        死信队列非空，需要人工处理：{{ JSON.stringify(retries.dead_by_kind || {}) }}
      </div>
    </div>
  </div>
</template>
