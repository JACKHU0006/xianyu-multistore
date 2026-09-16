"""
跨数据库后端兼容性测试

## 这些测试为什么存在

项目开发与 CI 都跑 SQLite，但生产用 PostgreSQL。两者行为并不一致，
最危险的是**在 SQLite 上通过、在 PG 上失败**的差异 —— 它们不会被现有测试发现。

本文件把已知差异固化成断言，**在两种后端上都跑**：

```bash
pytest tests/test_db_compat.py -q                          # SQLite
TEST_DATABASE_URL="postgresql+asyncpg://..." pytest tests/test_db_compat.py -q   # PostgreSQL
```

断言按后端分支：SQLite 上确认"已知的不一致行为"，PG 上确认"我们依赖的正确行为"。
这样任何一边的意外变化都会被发现。

## 为什么每个用例都在**一个**事件循环里跑完

asyncpg 的连接**绑定创建它的事件循环**。如果一个 engine 在循环 A 里建好连接，
之后在循环 B（`asyncio.run()` 会新建循环）里再用，就会抛
`RuntimeError: Event loop is closed` —— 而且报错点常常在无关的清理代码里，
很难看出真正原因。

`aiosqlite` 宽容得多，所以旧写法（fixture 里建 engine + 多次 `asyncio.run()`）
在 SQLite 上一直是绿的，切到 PG 才整体崩掉。因此这里统一改成：
**每个用例只调一次 `asyncio.run()`**，engine 的创建、建表、播种、断言、销毁
全在这一个循环内完成。用例需要多个会话时，在同一个 `scenario` 里开多个即可。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import String, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.models import Base, Order, Store, Tenant

from conftest import db_url, is_postgres


pytestmark = pytest.mark.skipif(
    False, reason="占位：本文件在两种后端上都要跑"
)


def _run_scenario(tmp_path: Path, scenario, *, name: str = "compat.db"):
    """在一个事件循环内完成：建 engine → 重建表 → 播种基础行 → 执行 scenario。

    scenario 收到的是 **engine** 而不是 session，这样需要多会话或需要断言
    "写入必须失败"的用例也能自己控制。engine 一定会被 dispose，
    否则连接会一直挂到 PG 上（跑全套时能累积到几十条空闲连接）。
    """
    url = db_url(tmp_path, name)

    async def main():
        engine = create_async_engine(url, pool_pre_ping=True)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)

            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                db.add(Tenant(id="t1", name="兼容性测试"))
                db.add(Store(id="s1", tenant_id="t1", name="测试店",
                             platform_account="base-acc", owner_name="测试"))
                await db.commit()

            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _sess(engine):
    """开一个会话。调用方负责 `async with`。"""
    return async_sessionmaker(engine, expire_on_commit=False)()


# ===========================================================================
# 一、时区往返（最可能导致生产事故的差异）
# ===========================================================================

def test_timezone_roundtrip_is_aware_on_postgres(tmp_path):
    """
    `DateTime(timezone=True)` 的往返语义。

    - PostgreSQL: TIMESTAMPTZ，读回来是 **aware**
    - SQLite: DATETIME 存字符串，**丢弃时区**，读回来是 naive

    业务代码里 `now(aware) - order.paid_at` 这种减法在两种情况下结果不同：
    aware 减 aware 正常；aware 减 naive 直接抛 TypeError。
    """
    paid = datetime(2026, 9, 15, 5, 0, tzinfo=timezone.utc)

    async def scenario(engine):
        async with _sess(engine) as db:
            db.add(Order(id="o-tz", tenant_id="t1", store_id="s1", product_id=None,
                         platform_order_id="PL-TZ", buyer_id="b1",
                         amount=100.0, status="PAID", paid_at=paid))
            await db.commit()
            db.expire_all()
            row = (await db.execute(select(Order).where(Order.id == "o-tz"))).scalar_one()
            return row.paid_at

    readback = _run_scenario(tmp_path, scenario)
    assert readback is not None

    if is_postgres():
        assert readback.tzinfo is not None, (
            "PostgreSQL 的 TIMESTAMPTZ 应返回 aware datetime；"
            "若这里是 naive，说明列类型被建成了 TIMESTAMP WITHOUT TIME ZONE"
        )
        # 时刻必须保持一致
        assert readback.astimezone(timezone.utc) == paid
    else:
        assert readback.tzinfo is None, (
            "SQLite 会丢弃时区。此断言是为了确认这个已知差异仍然存在 —— "
            "如果哪天变了，说明 SQLAlchemy 或驱动行为变了，需要重新评估时区处理"
        )


def test_aware_minus_readback_datetime_matches_backend(tmp_path):
    """
    验证"能不能做时间减法"，这是业务代码真实依赖的行为。

    它把风险显式化：如果后端读回 naive，而业务传进来的 now 是 aware，
    减法就会炸。测试按后端断言各自的真实结果。
    """
    paid = datetime(2026, 9, 15, 5, 0, tzinfo=timezone.utc)

    async def scenario(engine):
        async with _sess(engine) as db:
            db.add(Order(id="o-sub", tenant_id="t1", store_id="s1", product_id=None,
                         platform_order_id="PL-SUB", buyer_id="b1",
                         amount=100.0, status="PAID", paid_at=paid))
            await db.commit()
            db.expire_all()
            return (await db.execute(
                select(Order).where(Order.id == "o-sub"))).scalar_one().paid_at

    readback = _run_scenario(tmp_path, scenario)
    now = datetime(2026, 9, 15, 7, 0, tzinfo=timezone.utc)   # 业务侧总是 aware

    if is_postgres():
        waited = now - readback
        assert waited == timedelta(hours=2), "PG 上 aware 减 aware 应正常得到 2 小时"
    else:
        with pytest.raises(TypeError, match="offset-naive and offset-aware"):
            _ = now - readback


# ===========================================================================
# 二、字符串长度约束
# ===========================================================================

def test_overlong_string_is_rejected_only_on_postgres(tmp_path):
    """
    VARCHAR(n) 的执行力度。

    - SQLite: **完全不强制**，超长静默写入
    - PostgreSQL: **强制**，报 StringDataRightTruncation

    影响：平台推送的 buyer_id / msg_id 超长时，SQLite 上一切正常，
    切到 PG 才开始 500。入参层已加长度校验（见 test_api.py 第十一节），
    这里验证"数据库这一层"的行为差异本身。
    """
    too_long = "x" * 300     # platform_account 是 String(64)

    async def scenario(engine):
        async def write():
            async with _sess(engine) as db:
                db.add(Store(id="s-len", tenant_id="t1", name="超长测试",
                             platform_account=too_long, owner_name="测试"))
                await db.commit()

        if is_postgres():
            return ("raises", None)
        await write()
        async with _sess(engine) as db:
            row = (await db.execute(select(Store).where(Store.id == "s-len"))).scalar_one()
            return ("ok", len(row.platform_account))

    if is_postgres():
        # PG 必须在写入时就报错；用一个独立循环捕获异常，避免污染 scenario 的循环
        async def attempt():
            engine = create_async_engine(db_url(tmp_path, "compat.db"), pool_pre_ping=True)
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.drop_all)
                    await conn.run_sync(Base.metadata.create_all)
                async with _sess(engine) as db:
                    db.add(Tenant(id="t1", name="兼容性测试"))
                    await db.commit()
                async with _sess(engine) as db:
                    db.add(Store(id="s-len", tenant_id="t1", name="超长测试",
                                 platform_account=too_long, owner_name="测试"))
                    await db.commit()
            finally:
                await engine.dispose()

        with pytest.raises(Exception) as ei:
            asyncio.run(attempt())
        assert "too long" in str(ei.value).lower() or "truncat" in str(ei.value).lower()
    else:
        kind, length = _run_scenario(tmp_path, scenario)
        assert kind == "ok"
        assert length == 300, "SQLite 应原样存下超长字符串（不截断）"


# ===========================================================================
# 三、JSON 列往返
# ===========================================================================

def test_json_column_roundtrip(tmp_path):
    """JSON 列在两边都应正确往返成 Python 对象（list/dict），不能变成字符串。"""
    ladder = [0.05, 0.12, 0.23]

    async def scenario(engine):
        from backend.models import Product
        async with _sess(engine) as db:
            db.add(Product(id="p-json", tenant_id="t1", store_id="s1", item_id="IT-J",
                           title="JSON 测试", listed_price=100.0, min_price=80.0,
                           send_type="CARD_POOL", bargain_ladder=ladder))
            await db.commit()
            db.expire_all()
            return (await db.execute(
                select(Product).where(Product.id == "p-json"))).scalar_one().bargain_ladder

    got = _run_scenario(tmp_path, scenario)
    assert got == ladder, f"JSON 列应往返为 list，实际得到 {got!r}（类型 {type(got).__name__}）"
    assert isinstance(got, list), "不能是字符串"


# ===========================================================================
# 四、唯一约束冲突的表现
# ===========================================================================

def test_unique_violation_raises_integrity_error(tmp_path):
    """
    唯一约束冲突必须抛 SQLAlchemy 的 IntegrityError（方言无关的统一异常）。

    项目依赖这一点做幂等兜底（`except IntegrityError: return False`），
    如果某个后端抛出别的异常类型，幂等就会失效 —— 可能重复发货。
    """
    from sqlalchemy.exc import IntegrityError

    async def scenario(engine):
        async with _sess(engine) as db:
            db.add(Tenant(id="t-dup", name="重复"))
            await db.commit()
        async with _sess(engine) as db:
            db.add(Tenant(id="t-dup", name="重复2"))
            await db.commit()

    with pytest.raises(IntegrityError):
        _run_scenario(tmp_path, scenario)


# ===========================================================================
# 五、布尔值与数值精度
# ===========================================================================

def test_boolean_roundtrip(tmp_path):
    """布尔值必须往返成真正的 bool，不能变成 0/1 整数（PG 尤其要注意）。"""
    async def scenario(engine):
        async with _sess(engine) as db:
            db.add(Store(id="s-bool", tenant_id="t1", name="布尔测试",
                         platform_account="acc-bool", owner_name="测试",
                         auto_reply=False, auto_bargain=True))
            await db.commit()
            db.expire_all()
            return (await db.execute(select(Store).where(Store.id == "s-bool"))).scalar_one()

    row = _run_scenario(tmp_path, scenario)
    assert row.auto_reply is False
    assert row.auto_bargain is True
    assert isinstance(row.auto_reply, bool)


def test_float_price_precision(tmp_path):
    """
    价格必须精确保留两位小数。

    注意：模型用的是 Float(双精度)，不是 Numeric。
    Float 能精确表示 128.0、99.0 这类值；但涉及累加或除法时可能出现
    0.1+0.2 这类误差。如果以后要做金额对账，建议改用 Numeric(10,2)。
    """
    async def scenario(engine):
        async with _sess(engine) as db:
            db.add(Order(id="o-price", tenant_id="t1", store_id="s1", product_id=None,
                         platform_order_id="PL-P", buyer_id="b1",
                         amount=128.88, status="PAID"))
            await db.commit()
            db.expire_all()
            return (await db.execute(
                select(Order).where(Order.id == "o-price"))).scalar_one().amount

    assert _run_scenario(tmp_path, scenario) == 128.88


# ===========================================================================
# 六、文本排序与比较（中文场景要留意）
# ===========================================================================

def test_string_comparison_is_consistent(tmp_path):
    """
    字符串比较排序在两个后端上可能因 collation 不同而不同。

    这里只断言"能正常比较"，不假设具体顺序 ——
    因为 PG 的排序依赖数据库的 LC_COLLATE，与 SQLite 的 BINARY 排序不同。
    """
    async def scenario(engine):
        async with _sess(engine) as db:
            for i, name in enumerate(["b店", "a店", "c店"]):
                db.add(Store(id=f"s-sort{i}", tenant_id="t1", name=name,
                             platform_account=f"sort-acc-{i}", owner_name="测试"))
            await db.commit()
            rows = (await db.execute(select(Store.name).where(
                Store.id.like("s-sort%")).order_by(Store.name))).scalars().all()
            return list(rows)

    got = _run_scenario(tmp_path, scenario)
    assert sorted(got) == sorted(["a店", "b店", "c店"])   # 不假设顺序，只保证集合正确


# ===========================================================================
# 七、LIKE 大小写敏感性
# ===========================================================================

def test_like_is_case_sensitive_on_postgres(tmp_path):
    """
    LIKE 的大小写敏感性。

    - SQLite: 默认对 ASCII **不敏感**（'ABC' LIKE 'abc' 为真）
    - PostgreSQL: LIKE **敏感**（如需不敏感要用 ILIKE）

    影响：如果代码里用 `.like()` 做模糊匹配，两个后端结果会不同。
    项目目前在业务查询里用 `in_` / `==` 为主，不受影响，此处记录差异以便将来排查。
    """
    async def scenario(engine):
        async with _sess(engine) as db:
            db.add(Store(id="s-like", tenant_id="t1", name="ABC",
                         platform_account="CaseTest", owner_name="测试"))
            await db.commit()
            return (await db.execute(
                select(Store.id).where(Store.platform_account.like("casetest"))
            )).scalars().all()

    got = _run_scenario(tmp_path, scenario)
    if is_postgres():
        assert got == [], "PostgreSQL 的 LIKE 区分大小写，应匹配不到"
    else:
        assert got == ["s-like"], "SQLite 对 ASCII 的 LIKE 不区分大小写，应能匹配到"
