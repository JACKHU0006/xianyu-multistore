"""
测试全局夹具

## 为什么需要这个文件

原先各测试文件把数据库 URL 硬编码成 SQLite，导致一个盲区：
**测试只能在 SQLite 上跑，切到生产用的 PostgreSQL 之前无法验证。**

SQLite 与 PostgreSQL 在几处行为上并不一致，最典型的是：

  - **时区**：SQLite 的 DATETIME 只存字符串、**丢弃时区**，读回来是 naive datetime；
    PostgreSQL 的 TIMESTAMPTZ **保留时区**，读回来是 aware datetime。
    代码里 `now - order.paid_at` 这类减法在两种情况下行为不同。
  - **字符串长度**：SQLite **不强制** VARCHAR(n)，超长照样写入；
    PostgreSQL **强制**，超长直接报错。
  - **JSON**：PG 上是 JSON（非 JSONB），不支持包含查询与索引。

## 怎么切到 PostgreSQL 验证

```bash
# 默认：SQLite（快，无需外部依赖）
pytest tests/ -q

# 指向 PG —— 全套测试会在真实 PostgreSQL 上重跑一遍
export TEST_DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/xianyu_test"
pytest tests/ -q
```

> 注意：PG 模式下每个用例会**先 drop_all 再 create_all**，
> 所以 `TEST_DATABASE_URL` 必须指向**专用测试库**，绝不要指向生产库。

想临时起一个 PG：

```bash
docker run -d --name xy-pg -e POSTGRES_PASSWORD=postgres \\
  -e POSTGRES_DB=xianyu_test -p 5432:5432 postgres:16
```
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# 测试库连接串。未设置时用 SQLite。
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()


def is_postgres() -> bool:
    return TEST_DATABASE_URL.startswith(("postgresql", "postgres"))


def db_url(tmp_path: Path, name: str = "test.db") -> str:
    """
    返回测试数据库连接串。

    设置 TEST_DATABASE_URL 时统一用它（PG 模式，所有用例共用一个库，
    靠 drop_all/create_all 隔离）；否则每个用例一个独立的 SQLite 文件。
    """
    if TEST_DATABASE_URL:
        return TEST_DATABASE_URL
    return f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}"


@pytest.fixture(scope="session")
def database_backend() -> str:
    """让测试能按后端能力做分支（例如 SQLite 专属的行为断言）。"""
    return "postgresql" if is_postgres() else "sqlite"


@pytest.fixture(scope="session", autouse=True)
def _banner() -> None:
    """跑之前把后端打出来，避免误以为跑的是 PG。"""
    backend = "PostgreSQL" if is_postgres() else "SQLite"
    print(f"\n[conftest] 测试数据库后端：{backend}")
    if is_postgres():
        print(f"[conftest] URL：{TEST_DATABASE_URL}")
        print("[conftest] 提示：PG 模式下每个用例会 drop_all/create_all，请确认指向测试库。")
