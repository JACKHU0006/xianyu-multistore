<script setup>
import { computed, ref, watch } from 'vue'
import { api } from '@/api'
import { useSessionStore } from '@/stores/session'
import StatusPill from '@/components/StatusPill.vue'

const session = useSessionStore()
const inventory = ref([])
const loading = ref(false)

const VERDICT_TONE = {
  STOCKOUT: 'danger',
  STAR: 'ok',
  PRICING: 'warn',
  NEGOTIATION: 'warn',
  TRAFFIC: 'idle',
  NORMAL: 'idle',
}
const VERDICT_LABEL = {
  STOCKOUT: '缺货',
  STAR: '明星',
  PRICING: '定价问题',
  NEGOTIATION: '议价僵持',
  TRAFFIC: '曝光不足',
  NORMAL: '正常',
}

// 诊断需要"咨询/议价/成交"这类数据，库存接口里没有，
// 所以给一个可编辑的演示表 —— 真实场景应该从数据库按时间窗聚合。
const sampleProducts = ref([
  { product_id: 'p1', title: '爱奇艺黄金会员 年卡', inquiries: 20, bargains: 4, orders: 12, revenue: 1536, stock: 60, days_listed: 40 },
  { product_id: 'p2', title: 'Type-C 氮化镓充电器', inquiries: 50, bargains: 6, orders: 1, revenue: 79, stock: 12, days_listed: 60 },
  { product_id: 'p3', title: 'PS5 数字兑换码', inquiries: 2, bargains: 0, orders: 0, revenue: 0, stock: 30, days_listed: 25 },
])

const diagnoses = ref([])
const diagnosing = ref(false)

const levelTone = (level) => (level === 'OUT' ? 'danger' : level === 'LOW' ? 'warn' : 'ok')
const levelText = (level) => ({ OUT: '已断货', LOW: '偏低', OK: '正常' }[level] || level)

const hasStockAlert = computed(() => inventory.value.some((i) => i.level !== 'OK'))

async function load() {
  if (!session.currentStoreId) return
  loading.value = true
  try {
    const data = await api.inventory(session.currentStoreId)
    inventory.value = data.items || []
  } catch (err) {
    session.notify(err?.message || String(err), true)
  } finally {
    loading.value = false
  }
}

async function runDiagnose() {
  diagnosing.value = true
  try {
    const data = await api.diagnose(sampleProducts.value)
    diagnoses.value = data.items || []
  } catch (err) {
    session.notify(err?.message || String(err), true)
  } finally {
    diagnosing.value = false
  }
}

watch(() => session.currentStoreId, load, { immediate: true })
</script>

<template>
  <div v-if="hasStockAlert" class="banner danger">
    <div>
      <b>有商品库存告警</b>
      <p>断货的卡密类商品会被自动下架。补货后系统会自动恢复上架，但手动下架的商品不会自动上架。</p>
    </div>
  </div>

  <div class="card">
    <div class="card-h">
      <h3>库存与自动下架</h3>
      <span class="desc">只对卡密类商品扫描；人工发货与网盘商品没有库存概念</span>
      <div class="spacer" />
      <button class="btn sm" :disabled="loading" @click="load">刷新</button>
    </div>
    <div class="card-b" style="padding-top: 4px">
      <table v-if="inventory.length">
        <thead>
          <tr>
            <th>商品</th>
            <th>状态</th>
            <th>可用 / 阈值</th>
            <th>日均消耗</th>
            <th>预计可支撑</th>
            <th>优先级</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in inventory" :key="item.product_id">
            <td><div class="t1">{{ item.title }}</div></td>
            <td><StatusPill :tone="levelTone(item.level)">{{ levelText(item.level) }}</StatusPill></td>
            <td class="mono">{{ item.available }} / {{ item.threshold }}</td>
            <td>{{ item.daily_burn }}</td>
            <td>
              <span v-if="item.cover_days === null" class="dim">暂无消耗数据</span>
              <span v-else-if="item.cover_days < 1" style="color: var(--danger)">不足 1 天</span>
              <span v-else>约 {{ item.cover_days }} 天</span>
            </td>
            <td>
              <StatusPill :tone="item.priority === 'P0' ? 'danger' : item.priority === 'P1' ? 'warn' : 'idle'">
                {{ item.priority }}
              </StatusPill>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">该店铺没有卡密类商品</div>
    </div>
  </div>

  <div class="card">
    <div class="card-h">
      <h3>商品诊断</h3>
      <span class="desc">按「缺货 &gt; 明星 &gt; 曝光 &gt; 议价僵持 &gt; 定价」的顺序下结论；样本不足时不下结论</span>
      <div class="spacer" />
      <button class="btn sm primary" :disabled="diagnosing" @click="runDiagnose">运行诊断</button>
    </div>
    <div class="card-b" style="padding-top: 4px">
      <table>
        <thead>
          <tr>
            <th>商品</th>
            <th>咨询</th>
            <th>议价</th>
            <th>成交</th>
            <th>库存</th>
            <th>上架天数</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in sampleProducts" :key="p.product_id">
            <td><input v-model="p.title" /></td>
            <td><input class="w-num" type="number" v-model.number="p.inquiries" /></td>
            <td><input class="w-num" type="number" v-model.number="p.bargains" /></td>
            <td><input class="w-num" type="number" v-model.number="p.orders" /></td>
            <td><input class="w-num" type="number" v-model.number="p.stock" /></td>
            <td><input class="w-num" type="number" v-model.number="p.days_listed" /></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <div v-if="diagnoses.length" class="card">
    <div class="card-h"><h3>诊断结果</h3></div>
    <div class="card-b" style="padding-top: 4px">
      <table>
        <thead>
          <tr><th>商品</th><th>结论</th><th>依据</th><th>建议</th></tr>
        </thead>
        <tbody>
          <tr v-for="d in diagnoses" :key="d.product_id">
            <td class="t1">{{ d.title }}</td>
            <td><StatusPill :tone="VERDICT_TONE[d.verdict]">{{ VERDICT_LABEL[d.verdict] || d.verdict }}</StatusPill></td>
            <td class="muted">{{ d.detail }}</td>
            <td class="muted">{{ d.suggestion }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
