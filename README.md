# 多店铺 AI 客服与自动化运营系统

> 合规第一：本系统**不实现**账号矩阵、Cookie 池、人机验证码绕过、Xvfb 反检测等任何违反平台协议的功能。浏览器操作（人机验证）必须下沉到店主本机执行。

## 快速开始

```bash
# 1. 环境
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. 配密钥（32 字节 base64，不要提交进仓库）
cp .env.example .env
# 编辑 .env：填入 MASTER_KEY（可执行 python -c "from backend.crypto import generate_master_key; print(generate_master_key())" 生成）
# 生产环境还应设置 JWT_SECRET 以启用真实鉴权：
#   python -c "import secrets; print(secrets.token_urlsafe(48))"

# 3. 播种演示数据（含登录账号 owner / demo1234）
python -m backend.seed_demo

# 4. 起后端
python -m uvicorn backend.main:create_app --factory --host 127.0.0.1 --port 8000

# 5. 起前端（另开终端）
cd frontend
npm install
npm run dev

# 6. 打开 http://127.0.0.1:5173 —— 用 owner / demo1234 登录
```

### Docker 一键部署

```bash
cp .env.example .env  # 改 MASTER_KEY
# 生产：同时设 JWT_SECRET 并置 AUTH_MODE=jwt
# 可选：配 OPENAI_API_KEY / QWEN_VL_API_KEY 启用真实大模型
docker compose up --build
# 打开 http://localhost:5173
```

## 系统架构

```
浏览器 → nginx(前端) → /api 代理 → FastAPI 后端
                              ↓
                        ┌────┴────┬────────┬────────┐
                        ↓         ↓        ↓        ↓
                      SQLite   Redis    DeepSeek   Qwen-VL
                      (数据)   (幂等)   (文本议价) (多模态鉴真)
```

- **前端**：Vue3 + Pinia + Vue Router，vite 开发代理直连后端
- **后端**：FastAPI + SQLAlchemy 2.0 异步 ORM
- **数据库**：SQLite（开发）/ PostgreSQL（生产）
- **缓存**：Redis（可选，幂等去重加速；未配时退化为内存且 FailOpen）
- **大模型**：OpenAI 兼容接口（DeepSeek / Qwen），可选配置
- **部署**：docker-compose，前后端分离镜像

## 模块一览

| 模块 | 职责 |
|---|---|
| `api.py` | 15+ HTTP 接口，JWT 鉴权 + 登录 |
| `auth.py` | 纯标准库 JWT（HS256）+ PBKDF2 口令哈希 |
| `models.py` | SQLAlchemy 2.0 多租户模型 |
| `pipeline.py` | 消息处理管线：去重→拦截→FAQ→LLM→转人工→核算 |
| `guardrails.py` | 底价硬校验、卡密出库、信封加密调用 |
| `orders.py` | 订单状态机 + 纯函数对账 |
| `inventory.py` | 卡密库存监控 + 自动下架 |
| `off_platform_guard.py` | 站外引流拦截（零宽字符/全角/中文数字归一化对抗）|
| `handoff.py` | 转人工：权重信号 + SLA + 工单队列 |
| `crypto.py` | 信封加密：KEK→HKDF→DEK→AES-256-GCM，AAD 绑定店铺 |
| `llm_clients.py` | OpenAI 兼容客户端 + 多模态鉴真 |
| `rbac.py` | 四角色权限（OWNER/MANAGER/AGENT/VIEWER），能力/范围分离 |
| `audit.py` | 操作审计，只记变化字段，敏感值打码 |
| `alerting.py` | P0/P1/P2 分级告警路由 |
| `retry.py` | 补偿任务队列 + 死信 + 指数退避 |
| `ratelimit.py` | 按店铺令牌桶限流 |
| `analytics.py` | 转化漏斗 + 商品诊断 + 健康评分 |
| `sourcing.py` | 捡漏三级过滤（廉价→折扣→多模态），预算用尽降级放行 |
| `templates.py` | 三层规则继承（默认←租户←单店覆盖）|
| `refunds.py` | 退款自动决策（未发货→同意，已交付买家原因→拒绝）|
| `maintenance.py` | 数据留存 + 容量估算 + 重复率监控（免费断线探测器）|
| `adapters.py` | 平台 webhook 适配器（闲鱼等），原始格式 → 内部格式 + 签名校验 |
| `seed_demo.py` | 演示数据播种 |
| `mock_llm_server.py` | 本地大模型 mock（无 key 时验证议价管线）|

## 身份与鉴权

**默认走 JWT（HS256）**。请求头 `Authorization: Bearer <token>`，无 token 一律 `401`。
JWT 用 Python 标准库（`hmac` / `hashlib`）自己实现，**不引入额外依赖**；口令用
PBKDF2-HMAC-SHA256 加盐存储。

```http
POST /api/auth/login
{"username": "owner", "password": "demo1234"}

→ 200 {"access_token": "...", "token_type": "bearer",
       "expires_in": 43200, "role": "OWNER", "store_ids": ["s1","s2"]}
```

```http
GET /api/me
Authorization: Bearer <access_token>
```

### 两种模式

| `AUTH_MODE` | 行为 |
|---|---|
| `jwt`（**推荐/生产**）| 只认 Bearer token，无 token → 401 |
| `dev` | 退回请求头身份，**任何人伪造 `X-Role: OWNER` 即可提权** |

`main.py` 的取值规则：显式配了 `AUTH_MODE` 就照它走；没配但设了 `JWT_SECRET` 就自动
进 `jwt` 模式；两者都没有则退回 `dev` 并打印警告。

> ⚠️ **上线必须设 `JWT_SECRET`**。生成：`python -c "import secrets; print(secrets.token_urlsafe(48))"`

### 四角色能力差异

- **OWNER**：全部权限
- **MANAGER**：经营权限，但不能 MANAGE_USERS（不能给自己提权）
- **AGENT**：回复消息，看不到底价（服务端 redact）
- **VIEWER**：只读

## 核心 API

### 消息处理（议价 / FAQ / 拦截）

```http
POST /api/webhook/message
{
  "store_id": "s1",
  "buyer_id": "b1",
  "content": "能便宜点吗，125出吗",
  "item_id": "ITEM-1",
  "round_no": 1,
  "msg_id": "m1"
}
```

返回 `action` 类型：
- `FAQ_HIT` — FAQ 命中，零 LLM 成本
- `AI_REPLY` — AI 回复（含议价或普通回复）
- `DEFLECT` — 站外引流被拦截，自动兜底话术
- `HANDOFF` — 转人工，返回 ticket
- `DROP_DUPLICATE` — 重复消息丢弃

### 发货（卡密解密）

```http
POST /api/orders/{order_id}/ship?store_id=s1
```

返回 `ok:true` 时 `content` 为解密后的卡密；`ok:false` 时进入补偿重试队列。

### 平台 webhook 入口（真实平台接入）

把平台原始推送直接打进来，由适配器转换成内部格式：

```http
POST /api/webhook/xianyu?store_id=s1
X-Signature: <HMAC-SHA256(body, XIANYU_WEBHOOK_SECRET)>
{平台原始 JSON}
```

- 签名密钥从环境变量 `XIANYU_WEBHOOK_SECRET` 读取（未配时放行并告警，方便开发）
- 不处理的事件类型（如订单事件）返回 `204`，让平台别重试
- 新增平台只需在 `adapters.py` 的 `_REGISTRY` 里注册一个适配器类

### 健康分

```http
GET /api/analytics/health          # 全局跨店聚合
GET /api/analytics/health?store_id=s1  # 单店
```

六指标加权评分：FAQ 命中率、AI 处理占比、转人工率、P0 工单、重复率、缺货商品。

## 测试

```bash
python -m pytest tests/ -q
# 565 passed
```

- 单元测试：纯函数（对账、规则、加密、状态机、JWT）
- 集成测试：真实 HTTP 请求 + SQLite 文件库
- Mock 测试：httpx MockTransport 注入（超时、5xx、垃圾 JSON）

## 部署

详细的托管步骤见 **[docs/DEPLOY.md](docs/DEPLOY.md)**。核心结论：

| 组件 | 能否托管 | 选型 |
|---|---|---|
| 代码 | ✅ | GitHub（`.github/workflows/ci.yml` 自动跑测试 + 前端构建）|
| 前端 | ✅ | Vercel / Cloudflare Pages（`frontend/vercel.json` 已配）|
| 后端 | ✅ **但不要用 Serverless** | Render / Railway / Fly.io（`render.yaml` 已配，容器常驻）|
| 数据库 | ✅ | Supabase（Postgres）|
| Redis | ✅ | Upstash |

**为什么后端不能用 Vercel Serverless / Cloudflare Workers**：后端是**有状态常驻进程**
（内存里跑限流器、补偿/死信重试队列、工单队列、成本核算）。Serverless 冷启动即清零，
这些全部失效；SQLite 在 Serverless 也无持久盘，Cloudflare Workers 还跑不了 Python。

前端部署后把 `frontend/vercel.json` 里的 `REPLACE-WITH-YOUR-BACKEND` 换成后端域名，
浏览器同源访问 `/api`，**后端无需开 CORS**（这也更安全）。

## 合规声明

1. **不实现**账号矩阵 / Cookie 池 / 滑块与人机验证码绕过 / Xvfb 反检测伪装
2. 浏览器操作（人机验证）**必须下沉到店主本机**，云端只做 AI + 数据 + 调度
3. CORS 白名单**非 `*`**，避免控制台变成公开接口
4. 底价**服务端二次硬校验**，AI 无法通过 prompt 注入击穿
5. 卡密**信封加密**，AAD 绑定店铺，误写进别店行解不开

## 成本估算（3 店 / 150 条消息/天）

| 项目 | 用量 | 免费额度 | 占比 |
|---|---|---|---|
| Upstash Redis | ~1,050 req/天 | 10,000/天 | ~10% |
| Supabase 存储 | ~27MB/年 | 500MB | ~5% |
| 大模型（文本）| ¥0.11~0.27/天 | — | — |
| 大模型（多模态）| 1000+ tokens/图 | — | 成本大头 |

**FAQ 命中率 ≥60% 时，文本成本可忽略；多模态鉴真是真正成本驱动。**

## 生产检查清单

- [ ] `AUTH_MODE=jwt` 且 `JWT_SECRET` 已设（**否则任何人伪造 `X-Role: OWNER` 就能提权**）
- [ ] 无 Bearer 请求 `/api/me` 返回 **401**（而不是 200）
- [ ] `.env` 中 `MASTER_KEY` 已改（不要提交进仓库）
- [ ] `DATABASE_URL` 指向 PostgreSQL（非 SQLite）
- [ ] `REDIS_URL` / `REDIS_TOKEN` 已配（可选，建议配）
- [ ] `OPENAI_API_KEY` 或 `DEEPSEEK_API_KEY` 已配（启用真实议价）
- [ ] `CORS_ORIGINS` 已设为你的前端域名（非 `*`）
- [ ] `docker compose up --build` 能正常起全套
- [ ] `python -m pytest tests/` 全绿
- [ ] 演示数据已播种（`python -m backend.seed_demo`）
- [ ] GitHub CI 绿色

## License

MIT — 仅供学习和合规运营参考。
