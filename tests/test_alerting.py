"""
告警分级与路由测试

核心断言：P0 永远不静默、未确认要升级、值班表空洞要被发现。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.alerting import (
    Channel,
    Kind,
    Severity,
    Shift,
    build_digest,
    build_payload,
    channels_for,
    coverage_gaps,
    from_audit_entry,
    from_inventory_alert,
    from_reconcile_issue,
    from_ticket,
    make_alert,
    next_channels,
    next_on_call,
    on_call,
    overdue_for_ack,
    render,
    requires_ack,
    should_suppress,
)
from backend.audit import Action, build_entry
from backend.guardrails import TenantCtx
from backend.handoff import Reason, Ticket
from backend.inventory import STOCK_LOW, STOCK_OUT, InventoryAlert, alert_priority
from backend.orders import IssueKind, ReconcileIssue

NOW = datetime(2026, 9, 13, 19, 0, tzinfo=timezone.utc)


def p0(**kw):
    base = dict(severity=Severity.P0, kind=Kind.UNSHIPPED_TIMEOUT, store_id="s1",
                title="付款未发货", created_at=NOW)
    base.update(kw)
    return make_alert(**base)


def p1(**kw):
    base = dict(severity=Severity.P1, kind=Kind.STOCK_LOW, store_id="s1",
                title="库存偏低", created_at=NOW)
    base.update(kw)
    return make_alert(**base)


def p2(**kw):
    base = dict(severity=Severity.P2, kind=Kind.CRITICAL_CHANGE, store_id="s1",
                title="配置变更", created_at=NOW)
    base.update(kw)
    return make_alert(**base)


# ===========================================================================
# 一、路由
# ===========================================================================

def test_p0_goes_to_phone_and_im():
    assert channels_for(p0()) == (Channel.PHONE, Channel.IM)


def test_p1_goes_to_im_only():
    assert channels_for(p1()) == (Channel.IM,)


def test_p2_goes_to_digest_only():
    assert channels_for(p2()) == (Channel.DIGEST,)


def test_ack_required_only_for_p0_and_p1():
    assert requires_ack(p0()) and requires_ack(p1())
    assert not requires_ack(p2())


def test_unknown_severity_falls_back_to_im():
    a = make_alert(severity="P9", kind="X", store_id="s1", title="t")
    assert channels_for(a) == (Channel.IM,)


# ===========================================================================
# 二、静默窗口
# ===========================================================================

def test_p1_duplicate_within_window_is_suppressed():
    first = p1()
    again = p1(created_at=NOW + timedelta(minutes=5))
    assert should_suppress(again, [first], NOW + timedelta(minutes=5))


def test_p1_duplicate_outside_window_is_not_suppressed():
    first = p1()
    later = p1(created_at=NOW + timedelta(minutes=20))
    assert not should_suppress(later, [first], NOW + timedelta(minutes=20))


def test_p0_is_never_suppressed():
    # 同一条 P0 重复响是特性不是 bug
    first = p0()
    again = p0(created_at=NOW)
    assert not should_suppress(again, [first], NOW)


def test_different_dedupe_key_is_not_suppressed():
    other = p1(kind=Kind.STOCK_OUT)
    assert other.dedupe_key != p1().dedupe_key
    assert not should_suppress(other, [p1()], NOW + timedelta(minutes=1))


def test_dedupe_key_defaults_to_kind_plus_store():
    assert p1().dedupe_key == f"{Kind.STOCK_LOW}:s1"
    assert p1(store_id="s2").dedupe_key == f"{Kind.STOCK_LOW}:s2"


# ===========================================================================
# 三、升级
# ===========================================================================

def test_p1_overdue_after_thirty_minutes():
    a = p1()
    assert not overdue_for_ack(a, NOW + timedelta(minutes=29))
    assert overdue_for_ack(a, NOW + timedelta(minutes=31))


def test_acknowledged_alert_is_never_overdue():
    a = p1().acknowledged_by("u1", NOW + timedelta(minutes=2))
    assert a.acknowledged
    assert not overdue_for_ack(a, NOW + timedelta(hours=5))


def test_p2_never_escalates():
    assert not overdue_for_ack(p2(), NOW + timedelta(days=7))


def test_escalation_adds_phone_for_p1():
    a = p1()
    now = NOW + timedelta(minutes=31)
    assert next_channels(a, now, already_sent=(Channel.IM,)) == (Channel.PHONE,)


def test_no_escalation_before_the_deadline():
    assert next_channels(p1(), NOW + timedelta(minutes=5)) == ()


def test_no_duplicate_escalation_channel():
    a = p1()
    now = NOW + timedelta(minutes=31)
    assert next_channels(a, now, already_sent=(Channel.IM, Channel.PHONE)) == ()


def test_p0_escalates_after_five_minutes():
    a = p0()
    assert next_channels(a, NOW + timedelta(minutes=6), already_sent=(Channel.PHONE, Channel.IM)) == ()


# ===========================================================================
# 四、渲染与载荷
# ===========================================================================

def test_render_prefixes_by_severity():
    assert render(p0()).startswith("【紧急】")
    assert render(p1()).startswith("【预警】")
    assert render(p2()).startswith("【提示】")


def test_render_includes_detail():
    a = make_alert(severity=Severity.P0, kind="X", store_id="s1",
                   title="付款未发货", detail="已支付 45 分钟仍未发货", created_at=NOW)
    assert "45 分钟" in render(a)


def test_phone_payload_retries():
    payload = build_payload(p0(), Channel.PHONE, "13800000000")
    assert payload["type"] == "voice_call"
    assert payload["retry"] == 3


def test_im_payload_shape():
    payload = build_payload(p1(), Channel.IM, "ou_abc")
    assert payload["msg_type"] == "text"
    assert "text" in payload["content"]


def test_digest_payload_groups_items():
    payload = build_digest([p2(), p2(title="另一条")], recipient="ou_x")
    assert payload["count"] == 2
    assert len(payload["items"]) == 2


def test_empty_digest_says_so():
    assert build_digest([])["text"] == "今日无提示类告警"


# ===========================================================================
# 五、值班表
# ===========================================================================

def _shift(person, start_h, end_h, day=13):
    return Shift(person=person,
                 start=datetime(2026, 9, day, start_h, tzinfo=timezone.utc),
                 end=datetime(2026, 9, day, end_h, tzinfo=timezone.utc))


def test_shift_covers():
    s = _shift("张三", 9, 17)
    assert s.covers(NOW.replace(hour=10))
    assert not s.covers(NOW.replace(hour=18))


def test_on_call_finds_the_duty_person():
    shifts = [_shift("张三", 9, 17), _shift("李四", 17, 23)]
    assert on_call(shifts, NOW.replace(hour=10)) == "张三"
    assert on_call(shifts, NOW.replace(hour=20)) == "李四"


def test_on_call_returns_none_outside_shifts():
    assert on_call([_shift("张三", 9, 17)], NOW.replace(hour=3)) is None


def test_next_on_call_suggests_the_relief():
    shifts = [_shift("李四", 21, 23)]
    assert next_on_call(shifts, NOW) == "李四"


def test_coverage_gap_is_detected():
    # 9-12 和 14-18 之间空了 12-14
    shifts = [_shift("A", 9, 12), _shift("B", 14, 18)]
    gaps = coverage_gaps(shifts, _shift("x", 9, 18).start, _shift("x", 9, 18).end)
    assert len(gaps) == 1
    assert gaps[0] == (_shift("x", 12, 12).start, _shift("x", 14, 14).start)


def test_no_gap_when_fully_covered():
    shifts = [_shift("A", 9, 13), _shift("B", 13, 18)]
    assert coverage_gaps(shifts, _shift("x", 9, 18).start, _shift("x", 9, 18).end) == []


def test_trailing_gap_is_detected():
    gaps = coverage_gaps([_shift("A", 9, 17)], _shift("x", 9, 21).start, _shift("x", 9, 21).end)
    assert len(gaps) == 1
    assert gaps[0][0].hour == 17 and gaps[0][1].hour == 21


def test_empty_schedule_is_one_big_gap():
    gaps = coverage_gaps([], _shift("x", 9, 18).start, _shift("x", 9, 18).end)
    assert len(gaps) == 1


# ===========================================================================
# 六、从各模块产物构造告警
# ===========================================================================

def _inv_alert(level, available):
    return InventoryAlert(
        product_id="p1", store_id="s1", title="爱奇艺黄金会员 年卡", level=level,
        available=available, threshold=10, daily_burn=2.0, cover_days=1.0,
        priority=alert_priority(level, 1.0), should_pause=(level == STOCK_OUT),
    )


def test_from_inventory_alert_maps_level_to_kind():
    out = from_inventory_alert(_inv_alert(STOCK_OUT, 0), now=NOW)
    assert out.kind == Kind.STOCK_OUT
    assert out.severity == "P0"
    assert "已自动下架" in out.detail


def test_from_inventory_low_stock():
    low = from_inventory_alert(_inv_alert(STOCK_LOW, 8), now=NOW)
    assert low.kind == Kind.STOCK_LOW
    assert "剩余 8 条" in low.detail


def test_from_reconcile_issue_keeps_order_id_in_dedupe_key():
    issue = ReconcileIssue(IssueKind.OVER_SHIPPED, "P0", "PL-1", "本地已发货但平台未付款")
    a = from_reconcile_issue(issue, store_id="s1", now=NOW)
    assert a.severity == "P0"
    assert "PL-1" in a.dedupe_key


def test_from_ticket():
    ticket = Ticket(store_id="s1", buyer_id="b1", priority="P0", score=100,
                    reasons=(Reason.COMPLAINT,), opened_at=NOW)
    a = from_ticket(ticket, now=NOW)
    assert a.severity == "P0"
    assert Reason.COMPLAINT in a.detail


def test_from_audit_entry_only_for_sensitive_actions():
    ctx = TenantCtx(tenant_id="t1", user_id="u1")
    loud = build_entry(action=Action.CARD_EXPORT, ctx=ctx, actor_role="MANAGER",
                       target_type="card_pool", target_id="p1", store_id="s1", note="导出 200 条")
    quiet = build_entry(action=Action.LOGIN, ctx=ctx, actor_role="OWNER",
                        target_type="user", target_id="u1")

    assert from_audit_entry(loud, now=NOW) is not None
    assert from_audit_entry(quiet, now=NOW) is None
