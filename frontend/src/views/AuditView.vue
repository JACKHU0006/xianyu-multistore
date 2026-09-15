<script setup>
import { onMounted, ref } from 'vue'
import { api } from '@/api'
import { useSessionStore } from '@/stores/session'
import StatusPill from '@/components/StatusPill.vue'

const session = useSessionStore()

const filters = ref({ action: '', only_critical: false, limit: 100 })
const items = ref([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const data = await api.audit({
      store_id: session.currentStoreId || undefined,
      action: filters.value.action || undefined,
      only_critical: filters.value.only_critical || undefined,
      limit: filters.value.limit,
    })
    items.value = data.items || []
  } catch (err) {
    session.notify(err?.message || String(err), true)
  } finally {
    loading.value = false
  }
}

onMounted(load)

function changeSummary(changes) {
  if (!changes || !changes.length) return '—'
  return changes
    .map((c) => `${c.critical ? '!' : ''}${c.field}: ${fmt(c.before)} → ${fmt(c.after)}`)
    .join('；')
}

function fmt(value) {
  if (value === null || value === undefined) return '空'
  return String(value)
}

function isCriticalRow(row) {
  return (row.changes || []).some((c) => c.critical)
}
</script>

<template>
  <div class="card">
    <div class="card-h">
      <h3>操作审计</h3>
      <span class="desc">只记录真正变化的字段；敏感字段的值一律打码</span>
      <div class="spacer" />
      <input style="width: 180px" v-model="filters.action" placeholder="按动作筛选，如 MIN_PRICE_UPDATE" />
      <label class="row" style="gap: 6px; font-size: 12.5px; color: var(--muted)">
        <input type="checkbox" style="width: auto" v-model="filters.only_critical" />
        只看关键变更
      </label>
      <button class="btn sm" :disabled="loading" @click="load">查询</button>
    </div>
    <div class="card-b" style="padding-top: 4px">
      <table v-if="items.length">
        <thead>
          <tr><th>时间</th><th>操作人</th><th>动作</th><th>对象</th><th>变更</th></tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in items" :key="i">
            <td class="mono">{{ row.created_at ? row.created_at.replace('T', ' ').slice(0, 19) : '—' }}</td>
            <td>
              <div class="t1">{{ row.actor }}</div>
              <div class="t2">{{ row.role }}</div>
            </td>
            <td>
              <StatusPill :tone="isCriticalRow(row) ? 'warn' : 'idle'">{{ row.action }}</StatusPill>
            </td>
            <td class="mono">{{ row.target }}</td>
            <td class="muted" style="max-width: 420px">{{ changeSummary(row.changes) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">
        {{ loading ? '加载中…' : '暂无审计记录' }}
      </div>
    </div>
  </div>
</template>
