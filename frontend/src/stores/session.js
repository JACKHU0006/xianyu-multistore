import { defineStore } from 'pinia'
import { api, ApiError, clearToken, getIdentity, getToken, setIdentity, setToken } from '@/api'

/**
 * 会话与店铺上下文。
 *
 * 所有视图都从这里拿"当前店铺"，避免每个页面各自维护一份导致切店不同步。
 */
export const useSessionStore = defineStore('session', {
  state: () => ({
    identity: getIdentity(),
    token: getToken(),
    principal: null,
    stores: [],
    currentStoreId: '',
    ready: false,
    error: '',
    toast: null,
    toastTimer: null,
  }),

  getters: {
    currentStore: (s) => s.stores.find((x) => x.id === s.currentStoreId) || null,
    storeName: (s) => s.currentStore?.name || '未选择店铺',
    can: (s) => (perm) => (s.principal?.permissions || []).includes(perm),
  },

  actions: {
    async bootstrap() {
      try {
        const [principal, storeList] = await Promise.all([api.me(), api.stores()])
        this.principal = principal
        this.stores = storeList.items || []
        if (!this.stores.some((s) => s.id === this.currentStoreId)) {
          this.currentStoreId = this.stores[0]?.id || ''
        }
        this.ready = true
        this.error = ''
      } catch (err) {
        this.error = err instanceof ApiError ? err.message : String(err)
        this.ready = true
      }
    },

    selectStore(id) {
      this.currentStoreId = id
    },

    async login(username, password, tenantId) {
      const data = await api.login(username, password, tenantId)
      this.token = setToken(data.access_token)
      await this.bootstrap()
      return data
    },

    logout() {
      this.token = clearToken()
      this.principal = null
      this.stores = []
      this.currentStoreId = ''
    },

    applyIdentity(next) {
      this.identity = setIdentity(next)
    },

    /** 统一的请求包装：负责 loading、错误提示，避免每个视图各写一遍。 */
    async run(fn, { toast } = {}) {
      try {
        const result = await fn()
        if (toast) this.notify(toast)
        return result
      } catch (err) {
        const message = err instanceof ApiError ? err.message : String(err)
        this.notify(message, true)
        throw err
      }
    },

    notify(message, isError = false) {
      this.toast = { message, isError }
      if (this.toastTimer) clearTimeout(this.toastTimer)
      this.toastTimer = setTimeout(() => {
        this.toast = null
      }, 4200)
    },
  },
})
