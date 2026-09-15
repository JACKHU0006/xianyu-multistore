# 部署手册

面向**运维/实施人员**。目标：把系统从零部署到生产环境，并确认它真的可用。

- 只想在本地跑起来看效果 → 看 [README 快速开始](../README.md#快速开始)
- 想知道界面上每个功能怎么用 → 看 [操作手册](MANUAL.md)
- 想知道为什么这样选型 → 看 [部署指南](DEPLOY.md)（设计取舍）

---

## 目录

1. [部署前必读：三条硬约束](#一部署前必读三条硬约束)
2. [部署架构与组件清单](#二部署架构与组件清单)
3. [第一步：准备密钥](#三第一步准备密钥)
4. [第二步：数据库](#四第二步数据库)
5. [第三步：Redis（可选）](#五第三步redis可选)
6. [第四步：部署后端](#六第四步部署后端)
7. [第五步：部署前端](#七第五步部署前端)
8. [第六步：初始化数据与账号](#八第六步初始化数据与账号)
9. [上线验收清单](#九上线验收清单)
10. [日常运维](#十日常运维)
11. [故障排查](#十一故障排查)
12. [回滚与数据安全](#十二回滚与数据安全)

---

## 一、部署前必读：三条硬约束

这三条决定了架构，先读再动手，否则会返工。

### 约束 1：后端**不能**部署到 Serverless

后端是**有状态常驻进程**，内存里跑着这些：

| 组件 | 用途 | 冷启动后后果 |
|---|---|---|
| `LimiterRegistry` | 按店铺令牌桶限流 | 限流计数清零，突发流量挡不住 |
| `RetryQueue` | 发货失败补偿队列 + 死信 | **未完成的发货任务永久丢失** |
| `tickets` | 转人工工单队列 | 工单丢失，买家没人管 |
| `UsageTracker` | 成本核算、FAQ 命中率 | 指标归零，健康分失真 |
| `dedup` | 消息幂等去重 | 重复消息被重复处理，**可能重复发货** |

所以：**Vercel Serverless / Cloudflare Workers 一律不能用**。用容器常驻的 Render / Railway / Fly.io。另外 Cloudflare Workers 跑不了 Python。

### 约束 2：`MASTER_KEY` 丢了，卡密就永久解不开了

卡密用的是信封加密：`KEK(MASTER_KEY)` → `HKDF` → 每店独立 `DEK` → `AES-256-GCM`。
`MASTER_KEY` 不参与数据库存储，**只在环境变量里**。

- 备份 `MASTER_KEY`，与数据库备份分开存放
- 换 `MASTER_KEY` 会导致历史卡密全部无法解密
- 真的需要轮换，必须先导出全部卡密明文，再换密钥重新加密入库

### 约束 3：不设 `JWT_SECRET` 等于把后台公开

`AUTH_MODE` 的行为：

| 配置 | 行为 | 风险 |
|---|---|---|
| `JWT_SECRET` 已设 | 自动进入 `jwt` 模式，无 Bearer token 一律 401 | ✅ 安全 |
| 只设 `AUTH_MODE=jwt` | 同上，但登录接口会返回 503（没密钥签不出 token） | ⚠️ 不可用 |
| 两者都不设 | 退回 `dev` 模式，身份靠请求头 | ❌ **任何人伪造 `X-Role: OWNER` 即拿到全部权限** |

> 生产环境**必须**设置 `JWT_SECRET`。这是上线前第一件要确认的事。

---

## 二、部署架构与组件清单

```
                    ┌─────────────────┐
   浏览器  ────────▶│  Vercel / CF    │  前端静态站
                    │  (Vue3 构建)     │
                    └────────┬────────┘
                             │ /api/* 反向代理（同源，免 CORS）
                             ▼
                    ┌─────────────────┐
                    │  Render         │  FastAPI 容器（常驻）
                    │  Dockerfile     │
                    └───┬────────┬────┘
                        │        │
              ┌─────────▼──┐  ┌──▼──────────┐  ┌──────────────┐
              │ Supabase   │  │ Upstash     │  │ DeepSeek /   │
              │ PostgreSQL │  │ Redis(可选) │  │ Qwen(可选)   │
              └────────────┘  └─────────────┘  └──────────────┘
```

| 组件 | 必选 | 选型 | 免费额度 | 本项目需要 |
|---|---|---|---|---|
| 代码托管 | ✅ | GitHub | 无限公开库 | — |
| 后端 | ✅ | Render（容器） | 750h/月 | 1 个 Web Service |
| 数据库 | ✅ | Supabase | 500MB | ~30MB/年 |
| 前端 | ✅ | Vercel / Cloudflare Pages | 充足 | 静态托管 |
| Redis | ⬜ | Upstash | 10,000 命令/天 | ~1,050/天 |
| 大模型 | ⬜ | DeepSeek + Qwen-VL | 按量 | ¥0.11~0.27/天 |

**成本量级**：3 店 / 150 条消息/天的情况下，全部落在免费额度内，唯一变量是多模态鉴真的调用次数。

---

## 三、第一步：准备密钥

### 3.1 生成 `MASTER_KEY`

```bash
python -c "from backend.crypto import generate_master_key; print(generate_master_key())"
```

输出形如 `VpRWSl8NK4no7JC/EzCYK0AKU3PgsAA1HR30n98Fq+Q=`（32 字节 base64）。

> **立刻存进密码管理器。** 这个值丢失 = 所有历史卡密作废。

### 3.2 生成 `JWT_SECRET`

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 3.3 密钥存放原则

| 环境 | 存放位置 |
|---|---|
| 本地 | `.env` 文件（已被 `.gitignore` 挡住）|
| Render | 控制台 Environment 变量（`render.yaml` 里标 `sync: false` 的项）|
| 其他平台 | 该平台的 Secret / Environment 配置 |

**绝对不要**把密钥写进代码或提交进 Git。
如果曾经提交过，光删文件不够 —— 密钥已在 git 历史里，**必须轮换密钥**。

---

## 四、第二步：数据库

### 4.1 创建 Supabase 项目

1. https://supabase.com → New Project，选好区域（离用户近的）
2. 记下数据库密码
3. Settings → Database → Connection string → **URI** 复制

### 4.2 改驱动前缀

Supabase 给的是 `postgresql://`，本项目用异步驱动，必须改成 `postgresql+asyncpg://`：

```
postgresql+asyncpg://postgres:<密码>@db.<项目ref>.supabase.co:5432/postgres
```

> 密码里若有 `@` `#` `/` 等特殊字符，需要 URL 编码，否则会解析失败。

### 4.3 建表

在本地对生产库执行一次播种（`seed_demo` 内含 `create_all`）：

```bash
DATABASE_URL="postgresql+asyncpg://..." MASTER_KEY="<你的 MASTER_KEY>" \
  python -m backend.seed_demo
```

它会创建全部表并写入演示数据（租户 t1、账号 `owner`/`demo1234`、2 店、2 商品、各 2 张卡密）。

> **正式环境**：建议用 Alembic 迁移替代 `create_all`，并把演示数据清掉、改掉默认密码。
> `create_all` **只建不存在的表**，后续改表结构它不会同步，需人工处理。

### 4.4 确认连接池参数

本项目用 `create_async_engine(url, pool_pre_ping=True)`，默认池大小适合小规模。
连接数需要调，在 `DATABASE_URL` 后加参数即可：

```
...?pool_size=5&max_overflow=10
```

Supabase 免费版并发连接有限（约 60），**多实例部署时务必调小 `pool_size`**，否则会打满连接数。

---

## 五、第三步：Redis（可选）

不配也能跑：幂等去重会降级为进程内实现。

⚠️ **降级后的限制**：单实例可用；**多实例部署时同一消息可能被重复处理**，进而重复发货。生产环境建议配上。

### Upstash 配置

1. https://upstash.com → Create Database → Redis
2. 复制 **Endpoint** 与 **Password**
3. 本项目用标准 Redis 协议，连接串格式：

```
rediss://default:<password>@<endpoint>:6379
```

注意是 `rediss://`（双 s，TLS）。

> 幂等实现在 Redis 不可用时会 **FailOpen**（放行而非阻塞），保证可用性优先。
> 这意味着 Redis 挂了不会导致消息积压，但去重会暂时失效。

---

## 六、第四步：部署后端

### 6.1 用 Render Blueprint（推荐）

仓库根已有 `render.yaml`：

1. Render 控制台 → New → **Blueprint** → 选本仓库
2. Render 会读取 `render.yaml` 自动建服务
3. 按下表填写环境变量

| 变量 | 值 | 说明 |
|---|---|---|
| `AUTH_MODE` | `jwt` | blueprint 已设 |
| `JWT_SECRET` | 自动生成 | blueprint 用 `generateValue: true` 自动处理 |
| `MASTER_KEY` | 3.1 生成的值 | **必填** |
| `DATABASE_URL` | Supabase asyncpg 连接串 | **必填** |
| `CORS_ORIGINS` | 前端域名 | 见下方说明 |
| `REDIS_URL` | Upstash 连接串 | 可选 |
| `DEEPSEEK_API_KEY` | DeepSeek 密钥 | 可选，不配 AI 回复降级 |
| `QWEN_API_KEY` | 通义千问密钥 | 可选，不配则多模态鉴真放行 |
| `XIANYU_WEBHOOK_SECRET` | 平台 webhook 签名密钥 | 可选，不配则放行（仅开发）|

### 6.2 关于 `CORS_ORIGINS`

**分两种情况**：

- **前端反代 `/api` 到后端**（`frontend/vercel.json` 的默认做法）：浏览器看到的是同源请求，**CORS 不参与**，`CORS_ORIGINS` 填前端域名即可
- **前端直连后端域名**：必须把前端域名加进白名单

格式：逗号分隔，**不要写 `*`**。写 `*` 等于把控制台变成公开接口。

```
https://your-app.vercel.app,https://admin.yourdomain.com
```

### 6.3 可选的环境变量（大模型与预算）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | 文本模型地址 |
| `LLM_MODEL` | `deepseek-chat` | 文本模型名 |
| `LLM_API_KEY` | — | `DEEPSEEK_API_KEY` 的替代名 |
| `VISION_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 多模态地址 |
| `VISION_MODEL` | `qwen-vl-max` | 多模态模型名 |

> 任何 OpenAI 兼容接口都能接（改 `*_BASE_URL` + `*_MODEL` 即可）。
> 本地想验证议价管线但不想花钱，可以起 `mock_llm_server.py` 然后把
> `LLM_BASE_URL` 指向 `http://127.0.0.1:9099/v1`。

### 6.4 确认部署成功

```bash
curl https://<你的服务>.onrender.com/health
# {"status":"ok","time":"...","retries":{...},"limiters":{...}}
```

`/health` 无需鉴权，适合做健康检查探针（`render.yaml` 已配 `healthCheckPath`）。

### 6.5 用 Docker 自建（替代方案）

```bash
cp .env.example .env   # 填 MASTER_KEY、JWT_SECRET、DATABASE_URL
docker compose up --build
```

`Dockerfile` 是多阶段构建，`docker-compose.yml` 会同时起后端和前端 nginx。

---

## 七、第五步：部署前端

### 7.1 Vercel（推荐，配置已就绪）

1. New Project → 选仓库
2. **Root Directory 必须选 `frontend`** ← 最容易漏的一步
3. 框架自动识别 Vite；构建 `npm run build`，输出 `dist`
4. 部署前先改 `frontend/vercel.json`，把后端占位域名换掉：

```json
"rewrites": [
  { "source": "/api/:path*", "destination": "https://<你的后端>.onrender.com/api/:path*" },
  { "source": "/health",     "destination": "https://<你的后端>.onrender.com/health" }
]
```

这样做浏览器永远同源访问 `/api`，**后端无需开 CORS**，也更安全。

### 7.2 Cloudflare Pages

1. 连接仓库，Root directory 选 `frontend`，构建 `npm run build`，输出 `dist`
2. Pages 的 rewrites 需用 Functions，或改用 `VITE_API_BASE_URL` 直连后端
3. 若直连后端，**记得把前端域名加进后端 `CORS_ORIGINS`**

### 7.3 前端环境变量

| 变量 | 说明 |
|---|---|
| `VITE_API_BASE_URL` | 留空 = 同源（推荐）。填了则直连该地址 |
| `VITE_BACKEND_ORIGIN` | 仅开发用，vite 代理指向 |

> `VITE_` 前缀的变量会**打包进前端产物**，不要往里放任何密钥。

---

## 八、第六步：初始化数据与账号

### 8.1 演示数据包含什么

`python -m backend.seed_demo` 会写入：

| 对象 | 内容 |
|---|---|
| 租户 | `t1` |
| 账号 | `owner` / `demo1234`（OWNER，可见 s1、s2）|
| 店铺 | `s1` 会员卡券铺、`s2` 数码优选店 |
| 商品 | `p1` 爱奇艺年卡（128/底价 99）、`p2` 网易云季卡（45/底价 35）|
| FAQ | 各店 1~2 条 |
| 卡密 | 每店每商品 2 张（信封加密入库）|
| 订单 | `o1`(PL-1, 已发货)、`o2`(PL-2, 待发货) |

### 8.2 上线后必须做的三件事

1. **改掉 `owner` 的默认密码** —— `demo1234` 是公开的
2. **删掉演示数据**（商品、卡密、订单）
3. **按实际业务建店铺与商品**

> 目前没有用户管理界面，账号与密码需要用脚本在数据库里维护。
> 密码哈希格式（PBKDF2-HMAC-SHA256，20 万次迭代）：

```python
from backend.auth import hash_password
print(hash_password("你的新密码"))
# 把输出的字符串写进 users.password_hash 字段
```

---

## 九、上线验收清单

逐条打勾，**不要跳**。第 1、2 条是最关键的。

### 安全（必过）

- [ ] `JWT_SECRET` 已设置，且 `AUTH_MODE` 为 `jwt`
- [ ] **无 token 访问 `/api/me` 返回 401**（见下方命令）
- [ ] 用错误密码登录返回 401；正确密码能拿到 token
- [ ] `MASTER_KEY` 与本地不同，且**没有**进 Git 仓库
- [ ] `CORS_ORIGINS` 是具体域名，不是 `*`
- [ ] `.env` 未被提交（`git ls-files | grep .env` 应只有 `.env.example`）

```bash
# 最关键的一条：期望 401
curl -s -o /dev/null -w "%{http_code}\n" https://<后端域名>/api/me
```

> 若返回 **200**，说明 JWT 没生效、仍在 dev 模式，**后台等于公开**，立即处理。

### 功能（必过）

- [ ] `/health` 返回 `{"status":"ok"}`
- [ ] 能用 `owner` 登录前端，看到店铺矩阵
- [ ] 平台 webhook 签名（`XIANYU_WEBHOOK_SECRET`）已配，或确认平台不校验
- [ ] `DATABASE_URL` 指向 Postgres，不是 SQLite
- [ ] 多模态/文本大模型密钥已配（或确认接受降级行为）

### 观测（建议）

- [ ] GitHub CI 绿色
- [ ] 配了 Redis（多实例部署时**必须**配）
- [ ] 知道去哪儿看 `/api/metrics` 与 `/api/ops/maintenance`

---

## 十、日常运维

### 10.1 必须定期看的三项

| 接口 | 看什么 | 多久看 |
|---|---|---|
| `/api/metrics` | 成本、转人工队列积压 | 每天 |
| `/api/ops/maintenance` | 存储容量、重复消息率 | 每周 |
| `/api/analytics/health` | 健康分、问题清单 | 每周 |

**重复消息率是免费的断线探测器**：正常情况下应该接近 0。如果突然升高，通常意味着平台连接在重试，或消息被重复推送 —— 后者可能引发重复发货。

### 10.2 数据留存

`/api/ops/maintenance` 会返回各表建议的留存天数与是否需要归档。系统**不会自动删除**数据，需要按报告人工处理。

### 10.3 告警

后端有 P0/P1/P2 分级告警模块（`alerting.py`），但**出站通道需要自行接入**（未内置邮件/短信）。

建议至少接一条：把 `/api/metrics` 里的 `retries.needs_attention`、`handoff.breached`、`handoff.by_priority.P0` 做成定时探测。

### 10.4 备份

| 对象 | 方式 | 频率 |
|---|---|---|
| PostgreSQL | Supabase 自带每日备份 | 自动 |
| `MASTER_KEY` | 密码管理器 / 离线保管 | 一次，多处 |
| `.env` 配置 | 加密存放 | 变更时 |

> 数据库备份**不含** `MASTER_KEY`。两者都丢了，卡密就真的找不回来了。

---

## 十一、故障排查

### 启动失败

| 现象 | 原因 | 处理 |
|---|---|---|
| `未配置 DATABASE_URL` | 环境变量没设 | 补上 |
| `密钥服务不可用` (503) | `MASTER_KEY` 缺失或格式不对 | 确认是 32 字节 base64 |
| 启动成功但所有请求 404 | 容器装配失败，路由没注册 | 确认用 `create_app` 工厂方式启动 |
| `AUTH_MODE=dev` 警告 | 没设 `JWT_SECRET` | 生产环境必须设 |

### 运行期

| 现象 | 原因 | 处理 |
|---|---|---|
| 全部接口 401 | token 过期（默认 12h）或 `JWT_SECRET` 变更 | 重新登录 |
| 前端能开、接口全 404 | `vercel.json` 里的后端域名占位符没换 | 改 rewrites |
| 浏览器报 CORS | 用了直连模式但没配 `CORS_ORIGINS` | 加白名单，或改用同源反代 |
| 发货一直失败 | 卡密池空 / 解密失败 | 看 `/api/metrics` 的 `retries.dead` |
| `429 Too Many Requests` | 触发按店铺限流 | 正常保护，等重试窗口 |
| 消息重复处理 | Redis 未配且多实例 | 配 Redis |
| AI 不回复，`note: 未配置大模型客户端` | 没配模型密钥 | 配 key，或接受降级 |

### 如何定位失败的发货任务

```bash
curl -s https://<后端域名>/api/metrics -H "Authorization: Bearer <token>" | jq '.retries'
```

关注：
- `dead` > 0 → 有任务重试耗尽，需要人工介入
- `dead_by_kind` → 按任务类型分组，能看出是哪类操作在失败
- `needs_attention: true` → 需要立刻处理

---

## 十二、回滚与数据安全

### 代码回滚

```bash
git revert <commit>      # 生成反向提交，保留历史（推荐）
git push origin main
```

Render 会自动重新部署。也可在 Render 控制台直接选历史部署版本回滚。

### 数据库回滚

`create_all` 不支持回滚。改表结构前务必：

1. 确认 Supabase 有可用备份点
2. 变更前手动打一个快照
3. 生产环境引入 Alembic 做版本化迁移

### 密钥轮换

| 密钥 | 能否直接换 | 说明 |
|---|---|---|
| `JWT_SECRET` | ✅ 可随时换 | 影响：所有人被登出，需重新登录 |
| `MASTER_KEY` | ❌ **不能直接换** | 换了历史卡密全解不开，需先导出再重加密 |
| `*_WEBHOOK_SECRET` | ✅ 可换 | 需与平台侧同步修改 |

### 撤销泄露的凭据

若 `MASTER_KEY` 或大模型密钥泄露：

1. **先评估影响面**：泄露多久、谁可能拿到
2. `MASTER_KEY`：导出全部卡密明文 → 换新密钥 → 重新加密入库 → 作废旧卡密
3. 大模型密钥：到对应平台吊销重发
4. 检查 `audit_log` 有无异常操作

---

## 附录：完整环境变量速查

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `DATABASE_URL` | ✅ | — | 异步驱动连接串 |
| `MASTER_KEY` | ✅ | — | 32 字节 base64，卡密信封加密根密钥 |
| `JWT_SECRET` | ✅生产 | — | 设了自动进 jwt 模式 |
| `AUTH_MODE` | ⬜ | 有 secret 则 `jwt`，否则 `dev` | 显式覆盖 |
| `CORS_ORIGINS` | ⬜ | localhost:5173 | 逗号分隔，别写 `*` |
| `REDIS_URL` | ⬜ | 内存降级 | 幂等去重 |
| `REDIS_TOKEN` | ⬜ | — | Upstash 用 |
| `DEEPSEEK_API_KEY` | ⬜ | — | 文本议价 |
| `LLM_API_KEY` | ⬜ | — | 上者的别名 |
| `LLM_BASE_URL` | ⬜ | DeepSeek 官方 | OpenAI 兼容地址 |
| `LLM_MODEL` | ⬜ | `deepseek-chat` | |
| `QWEN_API_KEY` | ⬜ | — | 多模态鉴真 |
| `VISION_BASE_URL` | ⬜ | 阿里云 dashscope | |
| `VISION_MODEL` | ⬜ | `qwen-vl-max` | |
| `<平台>_WEBHOOK_SECRET` | ⬜ | — | 如 `XIANYU_WEBHOOK_SECRET` |
| `VITE_API_BASE_URL` | ⬜ | 空（同源）| 前端直连后才需要 |
