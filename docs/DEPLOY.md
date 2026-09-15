# 部署指南

## 先决结论：什么能上，什么不能上

| 组件 | 能不能托管 | 选型 |
|---|---|---|
| 代码 | ✅ | GitHub（CI 自动跑测试）|
| 前端 | ✅ | Vercel / Cloudflare Pages（纯静态）|
| 后端 | ✅ **但不要用 Serverless** | Render / Railway / Fly.io（容器常驻）|
| 数据库 | ✅ | Supabase（Postgres）|
| Redis | ✅ | Upstash |

**为什么后端不能用 Vercel Serverless / Cloudflare Workers：**
后端是**有状态常驻进程** —— 内存里跑着限流器、补偿/死信重试队列、工单队列、
成本核算与去重计数。Serverless 每次冷启动状态清零，这些全部失效；且 SQLite 在
Serverless 无持久盘；后台重试/告警任务也无法在 Serverless 里跑。Cloudflare Workers
还跑不了 Python/FastAPI。

## 零、安全前置（必须）

后端已实现真实 JWT 鉴权。**上线前必须设置 `JWT_SECRET`**（配了它就会自动进入
`jwt` 模式，没有 Bearer token 一律 401）。若不设，会退回 `dev` 模式 —— 那时身份靠
请求头 `X-Role: OWNER`，**任何人伪造即可拿到全部权限**。

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## 一、GitHub

```bash
git init
git add .
git commit -m "init"
git remote add origin <your-repo>
git push -u origin main
```

`.gitignore` 已经挡住 `.env`、`*.db`、`node_modules/`、`dist/`。
推送后 `.github/workflows/ci.yml` 会自动跑后端 589 项测试 + 前端构建。

> ⚠️ 如果曾经把 `.env` 提交过，光删文件不够 —— 密钥已在 git 历史里，必须**轮换密钥**
> （重新生成 `MASTER_KEY` 会导致已入库卡密解不开，需先导出）。

## 二、数据库（Supabase）

1. 新建项目 → Settings → Database → 复制连接串。
2. 改成 asyncpg 驱动：
   `postgresql+asyncpg://postgres:<password>@db.<ref>.supabase.co:5432/postgres`
3. 建表：本地对生产库执行一次
   ```bash
   DATABASE_URL="<上一步的连接串>" python -m backend.seed_demo
   ```
   （`seed_demo` 会 `create_all`；正式环境可用 Alembic 迁移替代。）
4. 建议同时开启 Row Level Security 作为第二道防线。

## 三、Redis（Upstash）

1. 新建 Redis 数据库 → 复制 `UPSTASH_REDIS_REST_URL` / token。
2. 本项目用的是标准 Redis 协议，填 `REDIS_URL=rediss://default:<token>@<host>:6379`。
3. 不配也能跑：幂等去重会降级为进程内实现（单实例可用，多实例会重复处理消息）。

## 四、后端（Render）

仓库根已有 `render.yaml`。

1. Render → New → **Blueprint** → 选仓库。
2. 按提示填 `sync: false` 的变量：

| 变量 | 值 |
|---|---|
| `MASTER_KEY` | 32 字节 base64（`python -c "from backend.crypto import generate_master_key; print(generate_master_key())"`）|
| `DATABASE_URL` | Supabase 的 asyncpg 连接串 |
| `REDIS_URL` | Upstash 连接串（可留空）|
| `CORS_ORIGINS` | 前端域名，如 `https://your-app.vercel.app`（**不要写 `*`**）|
| `DEEPSEEK_API_KEY` / `QWEN_API_KEY` | 大模型密钥（不配则 FAQ/规则仍工作，AI 回复降级）|
| `XIANYU_WEBHOOK_SECRET` | 平台 webhook 签名密钥（不配则放行，仅开发）|

`AUTH_MODE=jwt` 与 `JWT_SECRET` 由 blueprint 自动处理（后者随机生成）。

3. 部署完成后访问 `https://<service>.onrender.com/health` 应返回 `{"status":"ok"}`。

## 五、前端（Vercel 或 Cloudflare Pages）

**Vercel**
1. New Project → 选仓库 → **Root Directory 选 `frontend`**。
2. 构建命令 `npm run build`，输出目录 `dist`（`frontend/vercel.json` 已写好）。
3. 把 `frontend/vercel.json` 里的 `REPLACE-WITH-YOUR-BACKEND` 换成你的 Render 域名，
   这样浏览器同源访问 `/api`，**后端无需开 CORS**。

**Cloudflare Pages**
1. 连接仓库，Root directory 选 `frontend`，构建命令 `npm run build`，输出 `dist`。
2. Pages 的 rewrites 需用 Functions 或直接在构建时设 `VITE_API_BASE_URL` 指向后端域名，
   并相应把该前端域名加入后端 `CORS_ORIGINS`。

## 六、上线自检清单

- [ ] `AUTH_MODE=jwt` 且 `JWT_SECRET` 已设置
- [ ] 无 Bearer 请求 `/api/me` 返回 **401**（而不是 200）
- [ ] 用错误密码登录返回 401，正确密码能拿到 token
- [ ] `MASTER_KEY` 与本地不同，且**没有**进仓库
- [ ] `CORS_ORIGINS` 是具体域名，不是 `*`
- [ ] `DATABASE_URL` 指向 Postgres，不是 SQLite
- [ ] `/health` 返回 ok
- [ ] GitHub CI 绿色

## 七、本地复现生产鉴权

```bash
# 用同一个 .env 起后端，AUTH_MODE=jwt 时验证"没有 token 就进不去"
curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"owner","password":"demo1234"}'
# 拿 access_token 后：
curl -s http://127.0.0.1:8000/api/me -H "Authorization: Bearer <token>"
```
