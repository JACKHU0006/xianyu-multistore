<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '@/api'
import { useSessionStore } from '@/stores/session'
import StatCard from '@/components/StatCard.vue'
import StatusPill from '@/components/StatusPill.vue'

const session = useSessionStore()

const maintenance = ref(null)
const queue = ref(null)
const loading = ref(false)

const REFUND_REASONS = [
  { value: 'NOT_DELIVERED', label: '没收到货' },
  { value: 'CARD_INVALID', label: '卡密无效' },
  { value: 'CARD_ALREADY_USED', label: '卡密已被使用' },
  { value: 'WRONG_ITEM', label: '发错货' },
  { value: 'NO_LONGER_NEEDED', label: '不想要了' },
  { value: 'DUPLICATE_PURCHASE', label: '重复购买' },
  { value: 'OTHER', label: '其他' },
]

const refundForm = ref({
  reason: 'CARD_INVALID',
  delivered: true,
  card_revealed: true,
  duplicate_order: false,
  total_orders: 10,
  refunds: 1,
  off_platform_strikes: 0,
  complaints: 0,
})
const refundResult = ref(null)
const evaluating = ref(false)

const DECISION_TONE = { AUTO_APPROVE: 'ok', REVIEW: 'warn', AUTO_REJECT: 'danger' }
const DECISION_LABEL = { AUTO_APPROVE: '自动同意', REVIEW: '转人工', AUTO_REJECT: '自动拒绝' }

const quotaTone = computed(() => {
  const ratio = maintenance.value?.quota?.used_ratio ?? 0
  return ratio >= 0.7 ? 'danger' : ratio >= 0.4 ? 'warn' : 'ok'
})

async function load() {
  loading.value = true
  try {
    const [m, q] = await Promise.all([api.maintenance(), api.handoffQueue()])
    maintenance.value = m
    queue.value = q
  } catch (err) {
    session.notify(err?.message || String(err), true)
  } finally {
    loading.value = false
  }
}

async function evaluateRefund() {
  evaluating.value = true
  try {
    refundResult.value = await api.evaluateRefund(refundForm.value)
  } catch (err) {
    session.notify(err?.message || String(err), true)
  } finally {
    evaluating.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="stats" style="grid-template-columns: repeat(3, 1fr)">
    <StatCard
      label="人工队列"
      :value="queue?.summary?.open ?? '—'"
      :tone="queue?.summary?.needs_attention ? 'danger' : 'ok'"
      :detail="queue?.summary?.breached ? `已超时 ${queue.summary.breached} 单` : '均未超时'"
    />
    <StatCard
      label="存储占用"
      :value="maintenance ? `${maintenance.quota.used_mb}MB` : '—'"
      :tone="quotaTone"
      :detail="maintenance ? `配额 ${maintenance.quota.quota_mb}MB（${(maintenance.quota.used_ratio * 100).toFixed(1)}%）` : ''"
    />
    <StatCard
      label="重复消息率"
      :value="maintenance ? `${(maintenance.dedup.rate * 100).toFixed(1)}%` : '—'"
      :tone="maintenance?.dedup?.level === 'ALARMING' ? 'danger' : maintenance?.dedup?.level === 'ELEVATED' ? 'warn' : 'ok'"
      :detail="maintenance?.dedup?.level || ''"
    />
  </div>

  <div class="card">
    <div class="card-h">
      <h3>数据留存计划</h3>
      <span class="desc">按表区分保留期 —— 这不是清理，是让表保持可查</span>
      <div class="spacer" />
      <button class="btn sm" :disabled="loading" @click="load">刷新</button>
    </div>
    <div class="card-b" style="padding-top: 4px">
      <table v-if="maintenance">
        <thead><tr><th>表</th><th>保留天数</th><th>说明</th><th>需要归档</th></tr></thead>
        <tbody>
          <tr v-for="p in maintenance.retention" :key="p.table">
            <td class="mono">{{ p.table }}</td>
            <td>{{ p.retention_days ?? '永久' }}</td>
            <td class="muted">{{ p.reason }}</td>
            <td>
              <StatusPill :tone="p.actionable ? 'warn' : 'idle'">
                {{ p.actionable ? '是' : '否' }}
              </StatusPill>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">{{ loading ? '加载中…' : '无数据' }}</div>
    </div>
  </div>

  <div class="card">
    <div class="card-h">
      <h3>退款决策</h3>
      <span class="desc">
        已交付的虚拟商品不适用无理由退款；但卡密无效、未发货、重复支付一律自动退
      </span>
    </div>
    <div class="card-b">
      <div class="form-grid">
        <div class="field">
          <label>退款理由</label>
          <select v-model="refundForm.reason">
            <option v-for="r in REFUND_REASONS" :key="r.value" :value="r.value">{{ r.label }}</option>
          </select>
        </div>
        <div class="field"><label>历史订单数</label><input type="number" v-model.number="refundForm.total_orders" /></div>
        <div class="field"><label>历史退款数</label><input type="number" v-model.number="refundForm.refunds" /></div>
        <div class="field"><label>站外引流次数</label><input type="number" v-model.number="refundForm.off_platform_strikes" /></div>
        <div class="field"><label>投诉次数</label><input type="number" v-model.number="refundForm.complaints" /></div>
      </div>
      <div class="row" style="margin-top: 12px; gap: 16px">
        <label class="row" style="gap: 6px; font-size: 12.5px; color: var(--muted)">
          <input type="checkbox" style="width: auto" v-model="refundForm.delivered" /> 已发货
        </label>
        <label class="row" style="gap: 6px; font-size: 12.5px; color: var(--muted)">
          <input type="checkbox" style="width: auto" v-model="refundForm.card_revealed" /> 卡密已被查看
        </label>
        <label class="row" style="gap: 6px; font-size: 12.5px; color: var(--muted)">
          <input type="checkbox" style="width: auto" v-model="refundForm.duplicate_order" /> 重复支付
        </label>
        <div class="spacer" />
        <button class="btn primary" :disabled="evaluating" @click="evaluateRefund">
          {{ evaluating ? '评估中…' : '评估' }}
        </button>
      </div>

      <div v-if="refundResult" style="margin-top: 16px">
        <div class="kv">
          <span class="k">决策</span>
          <span class="v">
            <StatusPill :tone="DECISION_TONE[refundResult.decision]">
              {{ DECISION_LABEL[refundResult.decision] || refundResult.decision }}
            </StatusPill>
          </span>
        </div>
        <div class="kv"><span class="k">买家风险分</span><span class="v">{{ refundResult.abuse_score }}</span></div>
        <div class="kv"><span class="k">处理时限</span><span class="v">{{ refundResult.sla_minutes }} 分钟</span></div>
        <div class="kv"><span class="k">说明</span><span class="v muted" style="font-weight: 400">{{ refundResult.note }}</span></div>
      </div>
    </div>
  </div>
</template>
