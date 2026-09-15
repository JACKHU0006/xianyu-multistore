/**
 * 后端接口封装
 *
 * 身份通过请求头传递。真实部署时这里应该换成从登录态拿 JWT ——
 * 现在的写法只是把契约形状摆出来，方便本地联调。
 */

const BASE = import.meta.env.VITE_API_BASE_URL || ''

const STORAGE_KEY = 'xy.identity'

const DEFAULT_IDENTITY = {
  tenantId: 't1',
  userId: 'u1',
  role: 'OWNER',
  storeIds: 's1',
}

let identity = { ...DEFAULT_IDENTITY }

try {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved) identity = { ...identity, ...JSON.parse(saved) }
} catch {
  // 本地存储不可用时用默认身份，不该因此打不开页面
}

export function getIdentity() {
  return { ...identity }
}

export function setIdentity(next) {
  identity = { ...identity, ...next }
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(identity))
  } catch {
    // 忽略
  }
  return getIdentity()
}

// 登录后拿到的 JWT。有 token 就只发 Bearer，不再发可伪造的 X-* 头。
const TOKEN_KEY = 'xy.token'

let token = ''
try {
  token = localStorage.getItem(TOKEN_KEY) || ''
} catch {
  token = ''
}

export function getToken() {
  return token
}

export function setToken(next) {
  token = next || ''
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    // 忽略
  }
  return token
}

export function clearToken() {
  return setToken('')
}

function buildUrl(path, params) {
  if (!params) return BASE + path
  const clean = Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ''),
  )
  const qs = new URLSearchParams(clean).toString()
  return BASE + path + (qs ? `?${qs}` : '')
}

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
  }
}

async function request(path, { method = 'GET', body, params } = {}) {
  const headers = {}
  if (token) {
    // 已登录：只发 Bearer，后端在 jwt 模式下也只认它
    headers['Authorization'] = `Bearer ${token}`
  } else {
    // 未登录（本地 dev 模式）：退回可伪造的身份头，仅用于开发联调
    headers['X-Tenant-Id'] = identity.tenantId
    headers['X-User-Id'] = identity.userId
    headers['X-Role'] = identity.role
    if (identity.storeIds) headers['X-Store-Ids'] = identity.storeIds
  }
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let response
  try {
    response = await fetch(buildUrl(path, params), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (cause) {
    throw new ApiError('无法连接后端服务，请确认服务已启动', 0, { cause })
  }

  const text = await response.text()
  let payload = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = { detail: text }
    }
  }

  if (!response.ok) {
    const detail = payload?.detail
    const message = typeof detail === 'string' ? detail : `请求失败（HTTP ${response.status}）`
    throw new ApiError(message, response.status, payload)
  }
  return payload
}

export const api = {
  login: (username, password, tenantId) =>
    request('/api/auth/login', {
      method: 'POST',
      body: { username, password, tenant_id: tenantId || undefined },
    }),

  health: () => request('/health'),
  me: () => request('/api/me'),
  stores: () => request('/api/stores'),

  inventory: (storeId) => request(`/api/stores/${storeId}/inventory`),
  reconcile: (storeId, snapshot) =>
    request(`/api/stores/${storeId}/reconcile`, { method: 'POST', body: { snapshot } }),

  sendMessage: (payload) => request('/api/webhook/message', { method: 'POST', body: payload }),
  shipOrder: (orderId, storeId) =>
    request(`/api/orders/${orderId}/ship`, { method: 'POST', params: { store_id: storeId } }),

  audit: (params) => request('/api/audit', { params }),
  handoffQueue: () => request('/api/handoff/queue'),
  evaluateRefund: (body) => request('/api/refunds/evaluate', { method: 'POST', body }),

  metrics: (storeId) => request('/api/metrics', { params: { store_id: storeId } }),
  analyticsHealth: (storeId) => request('/api/analytics/health', { params: { store_id: storeId } }),
  diagnose: (products) => request('/api/analytics/diagnose', { method: 'POST', body: { products } }),
  sourcing: (storeId, body) =>
    request(`/api/stores/${storeId}/sourcing`, { method: 'POST', body }),
  maintenance: () => request('/api/ops/maintenance'),
}
