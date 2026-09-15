"""
迁移与模型一致性测试

## 目的

防止一类很隐蔽的故障：**改了 `models.py` 但忘了生成迁移**。

这类问题在开发期完全无感（`create_all` 会跟着模型走），
只有部署到生产、`alembic upgrade head` 之后才发现表结构还是旧的 ——
或者更糟，服务代码引用了一个数据库里不存在的列，线上直接 500。

本测试做两件事：
  1. 断言"迁移脚本建出的 schema" 与 "模型 create_all 建出的 schema" **完全一致**
  2. 断言 upgrade / downgrade 双向可用
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.environ.get("TEST_DATABASE_URL", "").startswith(("postgres", "postgresql")),
    reason="迁移一致性测试用 SQLite 文件库做 schema 比对；PG 上的迁移校验需单独用 alembic 命令跑",
)


def _drop(path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()


def _build_via_orm(db: Path) -> None:
    code = (
        "import asyncio, sys\n"
        f"sys.path.insert(0, r'{ROOT}')\n"
        "from sqlalchemy.ext.asyncio import create_async_engine\n"
        "from backend.models import Base\n"
        "async def main():\n"
        f"    e = create_async_engine('sqlite+aiosqlite:///{db.as_posix()}')\n"
        "    async with e.begin() as c:\n"
        "        await c.run_sync(Base.metadata.create_all)\n"
        "    await e.dispose()\n"
        "asyncio.run(main())\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)


def _alembic(args: list[str], db: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, DATABASE_URL=f"sqlite+aiosqlite:///{db.as_posix()}")
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )


def _schema(db: Path) -> dict:
    con = sqlite3.connect(db)
    out: dict[str, dict] = {}
    tables = [
        r[0] for r in con.execute(
            "select name from sqlite_master where type='table' "
            "and name != 'alembic_version' order by name"
        )
    ]
    for t in tables:
        cols = {
            r[1]: {"type": (r[2] or "").upper(), "notnull": bool(r[3]),
                   "default": r[4], "pk": bool(r[5])}
            for r in con.execute(f'PRAGMA table_info("{t}")')
        }
        idx = {}
        for r in con.execute(f'PRAGMA index_list("{t}")'):
            if r[1].startswith("sqlite_autoindex"):
                continue
            idx[r[1]] = [x[2] for x in con.execute(f'PRAGMA index_info("{r[1]}")')]
        out[t] = {"columns": cols, "indexes": idx}
    con.close()
    return out


def test_migration_matches_models(tmp_path):
    """
    核心断言：迁移脚本必须与 models.py 保持同步。

    失败时怎么修：
        alembic revision --autogenerate -m "描述这次的模型变更"
    然后把新生成的迁移文件一起提交。
    """
    db_orm = tmp_path / "orm.db"
    db_mig = tmp_path / "mig.db"
    _drop(db_orm)
    _drop(db_mig)

    _build_via_orm(db_orm)
    result = _alembic(["upgrade", "head"], db_mig)
    assert result.returncode == 0, f"alembic upgrade 失败：\n{result.stderr}"

    a, b = _schema(db_orm), _schema(db_mig)

    assert set(a) == set(b), (
        f"迁移与模型的表集合不一致。"
        f"仅模型有={sorted(set(a) - set(b))}，仅迁移有={sorted(set(b) - set(a))}\n"
        "→ 改了 models.py 后需要执行：alembic revision --autogenerate -m \"...\""
    )

    diffs: list[str] = []
    for t in sorted(a):
        if set(a[t]["columns"]) != set(b[t]["columns"]):
            diffs.append(
                f"[{t}] 列不一致：仅模型={sorted(set(a[t]['columns']) - set(b[t]['columns']))}，"
                f"仅迁移={sorted(set(b[t]['columns']) - set(a[t]['columns']))}"
            )
            continue
        for c in sorted(a[t]["columns"]):
            if a[t]["columns"][c] != b[t]["columns"][c]:
                diffs.append(f"[{t}.{c}] 模型={a[t]['columns'][c]} != 迁移={b[t]['columns'][c]}")
        if set(a[t]["indexes"]) != set(b[t]["indexes"]):
            diffs.append(
                f"[{t}] 索引不一致：仅模型={sorted(set(a[t]['indexes']) - set(b[t]['indexes']))}，"
                f"仅迁移={sorted(set(b[t]['indexes']) - set(a[t]['indexes']))}"
            )

    assert not diffs, "迁移与模型存在差异：\n" + "\n".join(f"  - {d}" for d in diffs)


def test_migration_is_reversible(tmp_path):
    """upgrade 之后必须能完整 downgrade，否则线上出问题无法回退。"""
    db = tmp_path / "rev.db"
    _drop(db)

    up = _alembic(["upgrade", "head"], db)
    assert up.returncode == 0, up.stderr
    assert _schema(db), "upgrade 之后应该有表"

    down = _alembic(["downgrade", "base"], db)
    assert down.returncode == 0, f"downgrade 失败（迁移不可回退）：\n{down.stderr}"
    assert not _schema(db), "downgrade base 之后业务表应被清空"


def test_existing_database_can_be_stamped(tmp_path):
    """
    已有数据库的接入路径。

    上线前就已经存在（且由 create_all 建出）的库，不应该再跑一次 upgrade ——
    那会因为表已存在而报错。正确做法是先 `stamp head` 打上基线标记。
    这个测试确保该流程可用。
    """
    db = tmp_path / "legacy.db"
    _drop(db)

    # 模拟"线上已有库"：只用 create_all 建表，没有任何 alembic 记录
    _build_via_orm(db)
    con = sqlite3.connect(db)
    has_version = con.execute(
        "select count(*) from sqlite_master where type='table' and name='alembic_version'"
    ).fetchone()[0]
    con.close()
    assert has_version == 0, "前提：该库此时不应有 alembic_version 表"

    # 打基线标记
    stamp = _alembic(["stamp", "head"], db)
    assert stamp.returncode == 0, f"stamp 失败：\n{stamp.stderr}"

    # 打完之后 upgrade 应该是空操作（已是最新）
    up = _alembic(["upgrade", "head"], db)
    assert up.returncode == 0, f"stamp 后 upgrade 失败：\n{up.stderr}"

    # 结构必须原样保留
    assert _schema(db), "stamp 不应破坏已有结构"
