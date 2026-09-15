"""
集成测试 —— 用 SQLite 内存库跑真实的 SQLAlchemy 会话。

验证三件在纯逻辑测试里覆盖不到的事：
  1. message_log 的 (store_id, platform_msg_id) 唯一约束真的挡得住重投
  2. 库存扫描能从真实数据里算出正确的分级
  3. 归零自动下架 / 补货自动恢复 真的改了数据库状态

注意：SQLite 不支持 FOR UPDATE SKIP LOCKED，所以发货出库那条路径
（guardrails.claim_card_and_ship）不在这里测，它需要 PostgreSQL。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.guardrails import TenantCtx
from backend.idempotency import IncomingMessage, persist_once
from backend.inventory import (
    STOCK_LOW,
    STOCK_OK,
    STOCK_OUT,
    apply_auto_pause,
    available_cards,
    resume_if_restocked,
    scan_store,
)
from backend.models import Base, CardPool, MessageLog, Product, ShipmentRecord, Store, Tenant

run = asyncio.run


async def _fresh_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(session, *, threshold=2, send_type="CARD_POOL"):
    tenant = Tenant(id="t1", name="演示团队")
    store = Store(id="s1", tenant_id="t1", name="会员卡券铺",
                  platform_account="dy_6640", owner_name="李四", status="ONLINE")
    product = Product(
        id="p1", tenant_id="t1", store_id="s1", item_id="721003311001",
        title="爱奇艺黄金会员 年卡", listed_price=128.0, min_price=99.0,
        send_type=send_type, low_stock_threshold=threshold,
        bargain_ladder=[0.05, 0.12, 0.23], auto_pause_on_empty=True,
    )
    session.add_all([tenant, store, product])
    await session.commit()
    return product


def _ctx():
    return TenantCtx(tenant_id="t1", user_id="u1")


async def _add_cards(session, n):
    for i in range(n):
        session.add(CardPool(
            tenant_id="t1", product_id="p1",
            content_encrypted=f"enc-{i}", content_hash=f"hash-{i}",
        ))
    await session.commit()


# ===========================================================================
# 一、数据库层幂等兜底
# ===========================================================================

def test_duplicate_platform_message_is_rejected_by_unique_constraint():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            await _seed(db)
            msg = IncomingMessage(buyer_id="b1", content="这个能便宜点吗", msg_id="m-777")

            first = await persist_once(db, _ctx(), "s1", msg, MessageLog)
            await db.commit()
            second = await persist_once(db, _ctx(), "s1", msg, MessageLog)

            count = await db.execute(
                MessageLog.__table__.select().where(MessageLog.platform_msg_id == "m-777")
            )
            rows = count.fetchall()
        await engine.dispose()
        return first, second, len(rows)

    first, second, rows = run(scenario())
    assert first is True
    assert second is False        # Redis 挂了也进不来第二条
    assert rows == 1


def test_different_stores_may_share_a_platform_message_id():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            await _seed(db)
            db.add(Store(id="s2", tenant_id="t1", name="数码优选店",
                         platform_account="dy_8821", owner_name="张三", status="ONLINE"))
            await db.commit()

            msg = IncomingMessage(buyer_id="b1", content="在吗", msg_id="m-shared")
            a = await persist_once(db, _ctx(), "s1", msg, MessageLog)
            await db.commit()
            b = await persist_once(db, _ctx(), "s2", msg, MessageLog)
            await db.commit()
        await engine.dispose()
        return a, b

    a, b = run(scenario())
    assert a is True and b is True   # 唯一约束是 (store_id, platform_msg_id)，不是全局


# ===========================================================================
# 二、库存扫描
# ===========================================================================

def test_scan_reports_out_of_stock_and_flags_pause():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            await _seed(db, threshold=2)
            alerts = await scan_store(db, _ctx(), "s1")
        await engine.dispose()
        return alerts

    alerts = run(scenario())
    assert len(alerts) == 1
    assert alerts[0].level == STOCK_OUT
    assert alerts[0].priority == "P0"
    assert alerts[0].should_pause is True
    assert alerts[0].cover_days is None      # 没有消耗记录，不算紧急天数


def test_scan_grades_by_threshold():
    async def scenario(threshold, cards):
        engine, Session = await _fresh_db()
        async with Session() as db:
            await _seed(db, threshold=threshold)
            await _add_cards(db, cards)
            alerts = await scan_store(db, _ctx(), "s1")
        await engine.dispose()
        return alerts[0]

    assert run(scenario(threshold=2, cards=0)).level == STOCK_OUT
    assert run(scenario(threshold=2, cards=2)).level == STOCK_LOW
    assert run(scenario(threshold=2, cards=3)).level == STOCK_OK


def test_burn_rate_escalates_healthy_stock_to_p0():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            await _seed(db, threshold=2)
            await _add_cards(db, 3)          # 3 条库存 > 阈值 2，分级是 OK
            now = datetime.now(timezone.utc)
            for i in range(70):              # 7 天卖掉 70 条 → 日均 10 条
                db.add(ShipmentRecord(
                    tenant_id="t1", store_id="s1", product_id="p1",
                    order_id=f"o-{i}", buyer_id="b1", status="SENT", shipped_at=now,
                ))
            await db.commit()
            alerts = await scan_store(db, _ctx(), "s1")
        await engine.dispose()
        return alerts[0]

    a = run(scenario())
    assert a.level == STOCK_OK           # 库存本身没到阈值
    assert a.daily_burn == 10.0
    assert a.cover_days == pytest.approx(0.3)
    assert a.priority == "P0"            # 但今天就会卖空 —— 静默卖超预警


def test_manual_shipping_products_are_not_scanned():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            await _seed(db, send_type="NONE")
            alerts = await scan_store(db, _ctx(), "s1")
        await engine.dispose()
        return alerts

    assert run(scenario()) == []


# ===========================================================================
# 三、自动下架 / 自动恢复
# ===========================================================================

def test_empty_pool_pauses_listing_once_only():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            product = await _seed(db, threshold=2)
            alert = (await scan_store(db, _ctx(), "s1"))[0]

            changed_first = await apply_auto_pause(db, product, alert)
            await db.commit()
            changed_second = await apply_auto_pause(db, product, alert)
            await db.commit()

            state = (product.is_active, product.stock_paused_at is not None)
        await engine.dispose()
        return changed_first, changed_second, state

    first, second, state = run(scenario())
    assert first is True
    assert second is False            # 第二次不改状态，避免每分钟重复推送
    assert state == (False, True)


def test_restock_resumes_listing():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            product = await _seed(db, threshold=2)
            alert = (await scan_store(db, _ctx(), "s1"))[0]
            await apply_auto_pause(db, product, alert)
            await db.commit()

            await _add_cards(db, 5)
            avail = await available_cards(db, _ctx(), "p1")
            resumed = await resume_if_restocked(db, product, avail)
            await db.commit()
            state = (product.is_active, product.stock_paused_at)
        await engine.dispose()
        return avail, resumed, state

    avail, resumed, state = run(scenario())
    assert avail == 5
    assert resumed is True
    assert state == (True, None)


def test_restock_does_not_resume_a_manually_delisted_product():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            product = await _seed(db, threshold=2)
            product.is_active = False      # 运营手动下架，stock_paused_at 保持为空
            await db.commit()

            await _add_cards(db, 5)
            resumed = await resume_if_restocked(db, product, 5)
            await db.commit()
            state = product.is_active
        await engine.dispose()
        return resumed, state

    resumed, state = run(scenario())
    assert resumed is False
    assert state is False              # 人的决定不该被自动逻辑推翻
