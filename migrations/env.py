"""
Alembic 环境配置

## 与项目其他部分的衔接

连接串与模型元数据都**从项目里取**，不在这里重复配置：

- URL：优先读环境变量 `DATABASE_URL`（与运行时同一套配置），
  没设时回落到 `alembic.ini` 里的 `sqlalchemy.url`。
- 元数据：直接 import `backend.models.Base.metadata`，
  这样 `alembic revision --autogenerate` 能自动发现模型变更。

## 常用命令

```bash
# 生成迁移（改完 models.py 后）
alembic revision --autogenerate -m "加 xxx 字段"

# 应用迁移
alembic upgrade head

# 回退一步
alembic downgrade -1

# 看当前版本
alembic current

# 看历史
alembic history --verbose
```

## 为什么用 async 模板

项目用的是 SQLAlchemy 2.0 的 asyncio 引擎（`asyncpg` / `aiosqlite`）。
Alembic 的同步引擎无法直接复用同一套 URL，所以用异步模板 +
`connection.run_sync()` 把迁移逻辑桥接到异步连接上。
"""

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# 让 alembic 能 import 到 backend 包（从仓库根执行时本来就能，这里兜底）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 的依据
target_metadata = Base.metadata


def _database_url() -> str:
    """
    取连接串。

    优先环境变量 `DATABASE_URL` —— 与 `backend.main.build_container()` 用同一个来源，
    避免"跑迁移连的是 A 库、起服务连的是 B 库"这种很容易犯的错。
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    return config.get_main_option("sqlalchemy.url") or ""


def _configure_kwargs() -> dict:
    return {
        "target_metadata": target_metadata,
        # 让 autogenerate 能察觉列类型的修改（默认只比较名称）
        "compare_type": True,
        # 也察觉 server_default 变化
        "compare_server_default": True,
    }


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连数据库。"""
    context.configure(
        url=_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_configure_kwargs(),
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **_configure_kwargs())
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
