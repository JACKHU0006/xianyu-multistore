# 数据库迁移手册（Alembic）

> 面向：负责升级线上库的开发/运维
> 一句话：**改模型必须写迁移，`create_all` 不会改已有表结构。**

---

## 一、为什么需要迁移

平台里有两套建表机制，职责不同：

| 机制 | 何时用 | 对已有表做什么 |
|---|---|---|
| `Base.metadata.create_all` | 首次建库、播种演示数据 | **什么都不做**（已存在的表直接跳过） |
| Alembic 迁移 | 模型变更后升级线上库 | `ALTER TABLE` / `CREATE` / `DROP` |

`create_all` 只补齐**缺失的表**。如果你给 `Product` 加了一列，线上库跑 `create_all`
不会有任何反应 —— 接口照常启动，然后在第一次 `SELECT` 那一列时抛
`column products.new_col does not exist`。这就是迁移存在的理由。

**约定：从今往后，改 `backend/models.py` 就必须同时产出一个迁移文件。**

### 版本标记：为什么播种后要 `stamp head`

`create_all` 不会写 `alembic_version` 表。如果库里没有版本行，运维第一次执行
`alembic upgrade head` 会把基线迁移**从头重放**，撞上已存在的表：

```
sqlalchemy.exc.OperationalError: table tenants already exists
```

而这个人只是想升级。所以 `python -m backend.seed_demo` 在 `create_all` 之后会自动
执行 `stamp head`，把库标记为「已处于基线」，后续 `upgrade` 就只跑真正的新迁移。

> 这一步是容错的：Alembic 没装、`alembic.ini` 缺失、或 stamp 超时，都只打印一行提示，
> 播种照常完成。播种是给人「马上有数据可玩」用的，不该被迁移工具拖垮。

**手工处理历史库**（比如线上已有数据的库，第一次接入 Alembic）：

```bash
export DATABASE_URL="postgresql+asyncpg://..."
python -m alembic stamp head   # 只写版本行，不动任何表
python -m alembic current      # 确认输出 8e1abc9e67e9 (head)
```

⚠️ `stamp` **只写版本号**。执行前请确认库结构与模型一致，否则等于「谎报军情」，
后续迁移会基于错误前提演进。不一致时用 `alembic check` 或下文的自查方法先核对。

---

## 二、日常流程

### 1. 改模型

编辑 `backend/models.py`，例如加一列：

```python
class Product(Base):
    ...
    stock_alert_threshold: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
```

### 2. 生成迁移

```bash
export DATABASE_URL="sqlite+aiosqlite:///./app.db"   # 或线上库（见下方警告）
python -m alembic revision --autogenerate -m "商品加库存预警阈值"
```

`migrations/env.py` 已开启：

- `compare_type=True` → 列类型变更也能被识别
- `compare_server_default=True` → 服务端默认值变更也能被识别
- `render_as_batch=True`（SQLite）→ 绕过 SQLite 不支持 `ALTER COLUMN` 的限制

> ⚠️ **autogenerate 是草稿不是成品**。它靠对比「模型元数据」和「目标库现状」生成 diff，
> 而目标库的现状取决于你连的是哪个库。生成后必须打开文件逐行读一遍，重点看：
> - 有没有把**无关的表**卷进来（说明本地库和模型早已漂移）
> - `nullable=False` 的新列**没有给 server_default** —— 已有行没法填，迁移会失败（见下）

### 3. 检查迁移文件

```bash
cat migrations/versions/<新文件>.py
```

### 4. 先在本地空库验证

```bash
rm -f _mig_test.db
DATABASE_URL="sqlite+aiosqlite:///./_mig_test.db" python -m alembic upgrade head
DATABASE_URL="sqlite+aiosqlite:///./_mig_test.db" python -m alembic downgrade base
DATABASE_URL="sqlite+aiosqlite:///./_mig_test.db" python -m alembic upgrade head
```

**必须验证 `downgrade`**。写不出 `downgrade` 的迁移等于没有回滚方案，出事时只能从备份恢复。

### 5. 跑测试

```bash
python -m pytest tests/test_migrations.py -q   # 结构一致性 + 可回滚 + 可 stamp
python -m pytest tests/ -q                     # 全量
```

`test_migration_matches_models` 会拿 `create_all` 出来的结构和 `upgrade head` 出来的
结构做指纹比对（表、列、索引）。**它红了通常意味着你改了模型但忘了生成迁移。**

### 6. 上线

```bash
export DATABASE_URL="postgresql+asyncpg://..."
python -m alembic current     # 记录升级前的版本，回滚要用
python -m alembic upgrade head
```

---

## 三、给已有数据的表加非空列

这是最容易出事的一类迁移。直接这么写会在已有行上报错：

```python
op.add_column("products", sa.Column("stock_alert_threshold", sa.Integer(), nullable=False))
# → column "stock_alert_threshold" of relation "products" contains null values
```

正确做法是拆三步（**新建三件事必须写在同一个迁移文件里**，否则中间态会有窗口期）：

```python
def upgrade() -> None:
    # 1) 先允许为空地加上
    op.add_column("products", sa.Column("stock_alert_threshold", sa.Integer(), nullable=True))
    # 2) 回填历史行
    op.execute("UPDATE products SET stock_alert_threshold = 0 WHERE stock_alert_threshold IS NULL")
    # 3) 再收紧为非空
    op.alter_column("products", "stock_alert_threshold", nullable=False)


def downgrade() -> None:
    op.drop_column("products", "stock_alert_threshold")
```

或者直接给 `server_default`，让数据库自己填：

```python
op.add_column(
    "products",
    sa.Column("stock_alert_threshold", sa.Integer(), nullable=False, server_default="0"),
)
```

## 四、SQLite 与 PostgreSQL 的差异（本平台踩过的坑）

本项目的测试默认跑 SQLite，生产跑 PostgreSQL。**在 SQLite 上通过的迁移，不代表在 PG 上能过。**
`tests/test_db_compat.py` 把这 9 条差异固化成了断言，跑 PG 时会走另一分支：

| 差异 | SQLite | PostgreSQL | 影响 |
|---|---|---|---|
| 时区 | `DateTime(timezone=True)` 存回来是 **naive** | 存回来是 **aware** | `aware - naive` 抛 `TypeError`；只在 PG 上复现 |
| 字符串长度 | **不校验**，`String(64)` 能塞 300 字符 | 严格拒绝 | 平台推来的超长字段在 PG 上会 500 |
| `LIKE` | 大小写不敏感 | **大小写敏感** | 关键词匹配结果两边不一致 |
| `JSON` | 存文本 | 原生 `json` | 都是 `list`，但排序/索引能力不同 |
| `ALTER COLUMN` | 不支持，需 batch 模式 | 支持 | `render_as_batch` 只对 SQLite 生效 |

> **时区这条是最阴的**：SQLite 测试之所以全绿，是因为测试里构造的 `now` 也是 naive 的，
> 两边一起错就抵消了。真要确认，必须跑一次 PG。

**跑一次真实 PostgreSQL 的完整验证：**

```bash
docker run -d --name xy-pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=xianyu_test -p 5432:5432 postgres:16

export TEST_DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/xianyu_test"
python -m pytest tests/ -q

docker rm -f xy-pg
```

> ⚠️ PG 模式下测试会 **drop_all / create_all**。`TEST_DATABASE_URL` 必须指向**专用测试库**，
> 绝不能指向有任何真实数据的库。`conftest.py` 的 `_banner` 夹具会在开始时把后端类型打出来。

---

## 五、常见错误

| 报错 | 原因 | 处理 |
|---|---|---|
| `table tenants already exists` | 库里有表但无 `alembic_version` | `alembic stamp head` 后再 `upgrade` |
| `Can't locate revision identified by 'xxx'` | 版本文件被删/分支不一致 | 查 `alembic history`，确认文件都在 |
| `Target database is not up to date` | 想生成迁移但库落后于 head | 先 `upgrade head`，再 `revision --autogenerate` |
| `contains null values` | 给已有表加非空列没回填 | 见第三节三步法 |
| `UnicodeDecodeError: 'gbk' codec ...` | 中文 Windows 上 `alembic.ini` 有非 ASCII 字节 | **`alembic.ini` 必须保持纯 ASCII**（Alembic 用 `encoding="locale"` 读它） |
| 迁移里出现了大量无关变更 | 本地库与模型早已漂移 | 用干净空库重新 autogenerate；不要直接提交 |

---

## 六、目录结构

```
alembic.ini                          # 纯 ASCII，勿加中文注释
migrations/
  env.py                             # 从 DATABASE_URL 读连接串，导入 Base.metadata
  script.py.mako                     # 新迁移文件模板
  versions/
    8e1abc9e67e9_initial_schema_baseline.py   # 基线：17 表 / 15 索引
tests/
  test_migrations.py                 # 结构一致性 / 可回滚 / 可 stamp
  test_db_compat.py                  # SQLite vs PG 差异
  conftest.py                        # TEST_DATABASE_URL 切换后端
```

### 设计说明：env.py 只认 `DATABASE_URL`

`migrations/env.py` 连库用的是**和运行时同一个环境变量** `DATABASE_URL`，
而不是 `alembic.ini` 里的 `sqlalchemy.url`。这样做的理由很直接：

> 如果迁移连 A 库、服务连 B 库，你会得到一个「迁移成功、但线上没生效」的假象 ——
> 这是最难查的一类故障，因为它每一步看起来都对。

`alembic.ini` 里那条 `sqlalchemy.url = sqlite+aiosqlite:///./app.db` 只是**兜底**，
方便在没设环境变量时也能跑 `alembic history` 之类的只读命令。
