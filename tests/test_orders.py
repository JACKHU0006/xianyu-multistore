"""
订单状态机与对账测试

重点覆盖两类会赔钱的事故：
  A. 发了货没付款（OVER_SHIPPED）
  B. 付款了没发货（UNSHIPPED_TIMEOUT / MISSING_LOCAL）
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.guardrails import TenantCtx
from backend.models import Base, Order, OrderStateLog, Store, Tenant
from backend.orders import (
    DEFAULT_UNSHIPPED_ALERT,
    IssueKind,
    InvalidTransition,
    OrderStatus as S,
    OrderView,
    apply_snapshot,
    can_ship,
    can_transition,
    find_stale_unshipped,
    is_terminal,
    load_local_views,
    reconcile,
    render_report,
    status_mismatch_severity,
    summarize,
    transition,
)

run = asyncio.run
NOW = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)


def view(oid, status, amount=100.0, paid_at=None, shipped_at=None):
    return OrderView(order_id=oid, status=status, amount=amount,
                     paid_at=paid_at, shipped_at=shipped_at)


def new_order(status=S.WAIT_BUYER_PAY, amount=100.0):
    return Order(
        id="o-uuid", tenant_id="t1", store_id="s1", product_id="p1",
        platform_order_id="PL-1", buyer_id="b1", amount=amount, status=status,
    )


# ===========================================================================
# 一、状态机
# ===========================================================================

def test_legal_transitions():
    assert can_transition(S.WAIT_BUYER_PAY, S.PAID)
    assert can_transition(S.PAID, S.SHIPPED)
    assert can_transition(S.SHIPPED, S.SUCCESS)
    assert can_transition(S.SHIPPED, S.REFUNDING)
    assert can_transition(S.REFUNDING, S.PAID)      # 退款被驳回，回到已支付


def test_illegal_transitions():
    assert not can_transition(S.WAIT_BUYER_PAY, S.SHIPPED)   # 没付款不能发货
    assert not can_transition(S.WAIT_BUYER_PAY, S.SUCCESS)
    assert not can_transition(S.REFUNDED, S.PAID)            # 终态不可复活
    assert not can_transition(S.CLOSED, S.PAID)
    assert not can_transition(S.PAID, S.SUCCESS)             # 不能跳过发货


def test_unknown_status_is_never_allowed():
    assert not can_transition("BANANA", S.PAID)
    assert not can_transition(S.PAID, "BANANA")


def test_only_paid_orders_can_ship():
    assert can_ship(S.PAID)
    for st in [S.WAIT_BUYER_PAY, S.SHIPPED, S.SUCCESS, S.REFUNDING, S.REFUNDED, S.CLOSED]:
        assert not can_ship(st), st


def test_terminal_statuses():
    assert is_terminal(S.REFUNDED) and is_terminal(S.CLOSED)
    assert not is_terminal(S.PAID)


def test_transition_stamps_paid_at():
    order = new_order()
    transition(order, S.PAID, actor="AI", now=NOW)
    assert order.status == S.PAID
    assert order.paid_at == NOW


def test_transition_does_not_overwrite_paid_at():
    order = new_order(S.PAID)
    order.paid_at = NOW - timedelta(hours=1)
    transition(order, S.SHIPPED, now=NOW)
    assert order.paid_at == NOW - timedelta(hours=1)
    assert order.shipped_at == NOW


def test_transition_stamps_closed_at_on_terminal():
    order = new_order(S.PAID)
    transition(order, S.CLOSED, reason="买家取消", now=NOW)
    assert order.closed_at == NOW


def test_transition_records_history():
    order = new_order()
    transition(order, S.PAID, actor="SYSTEM", now=NOW)
    transition(order, S.SHIPPED, actor="AI", reason="自动发货", now=NOW)
    assert [(log.from_status, log.to_status) for log in order.state_logs] == [
        (S.WAIT_BUYER_PAY, S.PAID), (S.PAID, S.SHIPPED),
    ]
    assert order.state_logs[1].actor == "AI"
    assert order.state_logs[1].reason == "自动发货"


def test_transition_refuses_illegal_and_leaves_state_untouched():
    order = new_order()  # WAIT_BUYER_PAY
    with pytest.raises(InvalidTransition):
        transition(order, S.SHIPPED)
    assert order.status == S.WAIT_BUYER_PAY
    assert order.state_logs == []


# ===========================================================================
# 二、对账
# ===========================================================================

def test_consistent_books_report_nothing():
    paid = NOW - timedelta(minutes=5)
    local = {"A": view("A", S.SHIPPED, paid_at=paid, shipped_at=NOW)}
    remote = {"A": view("A", S.SHIPPED, paid_at=paid, shipped_at=NOW)}
    assert reconcile(local, remote) == []


def test_platform_order_missing_locally_is_p0():
    issues = reconcile({}, {"A": view("A", S.PAID)})
    assert len(issues) == 1
    assert issues[0].kind == IssueKind.MISSING_LOCAL
    assert issues[0].severity == "P0"


def test_local_order_missing_on_platform_is_p1():
    issues = reconcile({"A": view("A", S.PAID)}, {})
    assert [i.kind for i in issues] == [IssueKind.MISSING_REMOTE]
    assert issues[0].severity == "P1"


def test_shipped_but_unpaid_is_over_shipped():
    # 事故 A：货发了，钱没到
    local = {"A": view("A", S.SHIPPED, shipped_at=NOW)}
    remote = {"A": view("A", S.WAIT_BUYER_PAY)}
    issues = reconcile(local, remote)
    assert [i.kind for i in issues] == [IssueKind.OVER_SHIPPED]
    assert issues[0].severity == "P0"


def test_shipped_but_refunded_is_over_shipped():
    local = {"A": view("A", S.SHIPPED, shipped_at=NOW)}
    remote = {"A": view("A", S.REFUNDED)}
    issues = reconcile(local, remote)
    assert issues[0].kind == IssueKind.OVER_SHIPPED


def test_platform_ahead_of_local_is_p0():
    # 平台已发货、本地还认为待发货 —— 下一轮扫描可能再发一次货
    local = {"A": view("A", S.PAID)}
    remote = {"A": view("A", S.SHIPPED)}
    issues = reconcile(local, remote)
    assert issues[0].kind == IssueKind.STATUS_MISMATCH
    assert issues[0].severity == "P0"


def test_local_ahead_of_platform_is_p1():
    local = {"A": view("A", S.SUCCESS)}
    remote = {"A": view("A", S.SHIPPED)}
    issues = reconcile(local, remote)
    assert issues[0].severity == "P1"


def test_amount_mismatch_is_p0():
    local = {"A": view("A", S.PAID, amount=99.0)}
    remote = {"A": view("A", S.PAID, amount=128.0)}
    issues = reconcile(local, remote)
    assert [i.kind for i in issues] == [IssueKind.AMOUNT_MISMATCH]
    assert issues[0].severity == "P0"


def test_amount_within_epsilon_is_ignored():
    local = {"A": view("A", S.PAID, amount=100.0)}
    remote = {"A": view("A", S.PAID, amount=100.005)}
    assert reconcile(local, remote) == []


def test_amount_mismatch_and_status_mismatch_both_reported():
    local = {"A": view("A", S.PAID, amount=99.0)}
    remote = {"A": view("A", S.SHIPPED, amount=128.0)}
    kinds = {i.kind for i in reconcile(local, remote)}
    assert kinds == {IssueKind.AMOUNT_MISMATCH, IssueKind.STATUS_MISMATCH}


def test_mismatch_severity_ignores_off_axis_statuses():
    assert status_mismatch_severity(S.SHIPPED, S.REFUNDING) == "P1"


def test_reconcile_sorts_p0_first():
    local = {"B": view("B", S.PAID)}
    remote = {"A": view("A", S.PAID), "B": view("B", S.PAID)}
    issues = reconcile(local, remote)
    assert issues[0].severity == "P0"


# ===========================================================================
# 三、发货超时
# ===========================================================================

def test_unshipped_within_threshold_is_quiet():
    orders = [view("A", S.PAID, paid_at=NOW - timedelta(minutes=5))]
    assert find_stale_unshipped(orders, now=NOW) == []


def test_unshipped_beyond_threshold_is_p0():
    orders = [view("A", S.PAID, paid_at=NOW - DEFAULT_UNSHIPPED_ALERT - timedelta(minutes=1))]
    issues = find_stale_unshipped(orders, now=NOW)
    assert issues[0].kind == IssueKind.UNSHIPPED_TIMEOUT
    assert issues[0].severity == "P0"
    assert "仍未发货" in issues[0].detail


def test_paid_without_timestamp_is_flagged():
    issues = find_stale_unshipped([view("A", S.PAID, paid_at=None)], now=NOW)
    assert len(issues) == 1
    assert "缺少支付时间" in issues[0].detail


def test_non_paid_orders_are_skipped():
    orders = [
        view("A", S.SHIPPED, paid_at=NOW - timedelta(days=1)),
        view("B", S.CLOSED, paid_at=NOW - timedelta(days=1)),
    ]
    assert find_stale_unshipped(orders, now=NOW) == []


def test_summarize_counts_and_flags_p0():
    issues = reconcile({"A": view("A", S.PAID)}, {"A": view("A", S.SHIPPED)})
    stats = summarize(issues)
    assert stats["total"] == 1 and stats["has_p0"] is True
    assert stats["by_severity"] == {"P0": 1}


def test_summarize_on_clean_books():
    stats = summarize([])
    assert stats == {"total": 0, "by_severity": {}, "by_kind": {}, "has_p0": False}


def test_render_report_clean_and_dirty():
    assert "一致" in render_report([], "会员卡券铺")
    text = render_report(reconcile({}, {"A": view("A", S.PAID)}), "会员卡券铺")
    assert "共 1 项" in text and "[P0]" in text


# ===========================================================================
# 四、数据库层
# ===========================================================================

async def _fresh_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(session):
    session.add_all([
        Tenant(id="t1", name="演示团队"),
        Store(id="s1", tenant_id="t1", name="会员卡券铺",
              platform_account="dy_6640", owner_name="李四", status="ONLINE"),
    ])
    await session.commit()


def _ctx():
    return TenantCtx(tenant_id="t1", user_id="u1")


def test_snapshot_creates_missing_orders():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            await _seed(db)
            res = await apply_snapshot(db, _ctx(), "s1", [view("PL-1", S.PAID)], now=NOW)
            await db.commit()
            views = await load_local_views(db, _ctx(), "s1")
        await engine.dispose()
        return res, views

    res, views = run(scenario())
    assert res.created == 1
    assert views["PL-1"].status == S.PAID


def test_snapshot_advances_legal_transition():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            await _seed(db)
            await apply_snapshot(db, _ctx(), "s1", [view("PL-1", S.PAID)], now=NOW)
            await db.commit()
            res = await apply_snapshot(db, _ctx(), "s1", [view("PL-1", S.SHIPPED)], now=NOW)
            await db.commit()
            views = await load_local_views(db, _ctx(), "s1")
        await engine.dispose()
        return res, views

    res, views = run(scenario())
    assert res.advanced == 1 and res.rejected == []
    assert views["PL-1"].status == S.SHIPPED


def test_snapshot_refuses_illegal_transition_and_reports():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            await _seed(db)
            await apply_snapshot(db, _ctx(), "s1", [view("PL-1", S.PAID)], now=NOW)
            await db.commit()
            # 平台说"关闭"，但本地已经发货了 —— 不允许被外部数据直接改写
            order = (await db.execute(
                select(Order).where(Order.platform_order_id == "PL-1")
            )).scalar_one()
            transition(order, S.SHIPPED, actor="AI", now=NOW)
            await db.commit()

            res = await apply_snapshot(db, _ctx(), "s1", [view("PL-1", S.CLOSED)], now=NOW)
            await db.commit()
            views = await load_local_views(db, _ctx(), "s1")
        await engine.dispose()
        return res, views

    res, views = run(scenario())
    assert res.advanced == 0
    assert len(res.rejected) == 1
    assert res.rejected[0].severity == "P0"
    assert views["PL-1"].status == S.SHIPPED   # 本地状态未被污染


def test_duplicate_platform_order_is_rejected():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            await _seed(db)
            await apply_snapshot(db, _ctx(), "s1", [view("PL-1", S.PAID)], now=NOW)
            await db.commit()
            db.add(Order(tenant_id="t1", store_id="s1", platform_order_id="PL-1",
                         buyer_id="b1", amount=100.0, status=S.PAID))
            with pytest.raises(IntegrityError):
                await db.flush()
            await db.rollback()
        await engine.dispose()

    run(scenario())
