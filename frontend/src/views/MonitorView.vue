<script setup>
import { computed, ref } from 'vue'
import { api } from '@/api'
import { useSessionStore } from '@/stores/session'
import StatusPill from '@/components/StatusPill.vue'

const session = useSessionStore()

const form = ref({
  keyword: 'Switch OLED',
  market_price: 1500,
  min_price: 1000,
  max_price: 1800,
  min_desire: 5,
  vision_discount_threshold: 0.15,
  vision_daily_budget: 5,
})

const SAMPLE = [
  { item_id: 'a1', title: 'Switch OLED 日版 9成新', price: 1200, desire_count: 26, description: '自用机，无划痕' },
  { item_id: 'a2', title: 'Switch OLED 港版 全新未拆', price: 1150, desire_count: 41, description: '朋友送的用不上' },
  { item_id: 'a3', title: 'Switch OLED 日版', price: 1450, desire_count: 18, description: '轻微使用痕迹' },
  { item_id: 'a4', title: 'Switch OLED 套装', price: 3000, desire_count: 30, description: '含配件全套' },
  { item_id: 'a5', title: 'Switch OLED 日版', price: 1200, desire_count: 2, description: '刚上架' },
]

const listingsText = ref(JSON.stringify(SAMPLE, null, 2))
const result = ref(null)
const running = ref(false)
const parseError = ref('')

const rejectTone = (reason) => (reason === 'VISION_RISKY' ? 'danger' : 'warn')

const costHint = computed(() => {
  const calls = result.value?.vision_calls ?? 0
  return `约 ¥${((result.value?.vision_cost ?? 0)).toFixed(3)}（单次估算 ¥0.02）`
})

function resetSample() {
  listingsText.value = JSON.stringify(SAMPLE, null, 2)
  parseError.value = ''
  result.value = null
}

async function run() {
  parseError.value = ''
  let listings
  try {
    listings = JSON.parse(listingsText.value)
    if (!Array.isArray(listings)) throw new Error('顶层必须是数组')
  } catch (err) {
    parseError.value = `商品列表 JSON 解析失败：${err.message}`
    return
  }

  running.value = true
  try {
    result.value = await api.sourcing(session.currentStoreId, {
      ...form.value,
      market_price: Number(form.value.market_price),
      listings,
    })
  } catch (err) {
    session.notify(err?.message || String(err), true)
  } finally {
    running.value = false
  }
}
</script>

<template>
  <div class="banner">
    <div>
      <b>捡漏监控 · 只做信息聚合</b>
      <p>
        按关键词抓取公开在售信息，经本地规则过滤后，只对「折扣足够大」的商品才调用多模态鉴真。
        仅推送提醒，不代拍、不代付。
      </p>
    </div>
  </div>

  <div class="card">
    <div class="card-h">
      <h3>监控参数</h3>
      <span class="desc">多模态是整条链路里最贵的一步，所以折扣门槛是成本控制的关键</span>
    </div>
    <div class="card-b">
      <div class="form-grid">
        <div class="field"><label>关键词</label><input v-model="form.keyword" /></div>
        <div class="field"><label>同款均价（元）</label><input type="number" v-model.number="form.market_price" /></div>
        <div class="field"><label>价格下限</label><input type="number" v-model.number="form.min_price" /></div>
        <div class="field"><label>价格上限</label><input type="number" v-model.number="form.max_price" /></div>
        <div class="field"><label>最低「几人想要」</label><input type="number" v-model.number="form.min_desire" /></div>
        <div class="field">
          <label>鉴真折扣门槛（0.15 = 便宜 15% 才看）</label>
          <input type="number" step="0.01" v-model.number="form.vision_discount_threshold" />
        </div>
        <div class="field">
          <label>每日鉴真预算（次）</label>
          <input type="number" v-model.number="form.vision_daily_budget" />
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-h">
      <h3>候选商品</h3>
      <span class="desc">真实场景由抓取任务写入，这里可手工调整用于验证过滤链</span>
      <div class="spacer" />
      <button class="btn sm" @click="resetSample">填入示例</button>
    </div>
    <div class="card-b">
      <textarea v-model="listingsText" rows="12" spellcheck="false" />
      <div v-if="parseError" class="hint" style="color: var(--danger); margin-top: 8px">{{ parseError }}</div>
      <div class="row" style="margin-top: 12px">
        <button class="btn primary" :disabled="running || !session.currentStoreId" @click="run">
          {{ running ? '筛选中…' : '运行筛选' }}
        </button>
        <span class="hint">
          需要「捡漏监控」权限；当前身份 {{ session.principal?.role }}，
          {{ session.can('monitor.manage') ? '有权限' : '无权限' }}
        </span>
      </div>
    </div>
  </div>

  <template v-if="result">
    <div class="stats" style="grid-template-columns: repeat(4, 1fr)">
      <div class="stat"><div class="k">通过</div><div class="v ok">{{ result.accepted.length }}</div></div>
      <div class="stat"><div class="k">淘汰</div><div class="v">{{ result.rejected.length }}</div></div>
      <div class="stat">
        <div class="k">多模态调用</div>
        <div class="v">{{ result.vision_calls }}</div>
        <div class="d">{{ costHint }}</div>
      </div>
      <div class="stat">
        <div class="k">跳过鉴真</div>
        <div class="v" :class="result.budget_exhausted ? 'warn' : ''">{{ result.vision_skipped }}</div>
        <div class="d">{{ result.budget_exhausted ? '预算已用尽，降级放行' : '折扣不足' }}</div>
      </div>
    </div>

    <div class="card">
      <div class="card-h"><h3>结果明细</h3></div>
      <div class="card-b">
        <div class="pre">{{ result.text }}</div>
      </div>
    </div>

    <div class="card">
      <div class="card-h"><h3>淘汰明细</h3></div>
      <div class="card-b" style="padding-top: 4px">
        <table v-if="result.rejected.length">
          <thead><tr><th>商品 ID</th><th>原因</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="r in result.rejected" :key="r.item_id">
              <td class="mono">{{ r.item_id }}</td>
              <td><StatusPill :tone="rejectTone(r.reason)">{{ r.reason }}</StatusPill></td>
              <td class="muted">{{ r.detail }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">没有淘汰项</div>
      </div>
    </div>
  </template>
</template>
