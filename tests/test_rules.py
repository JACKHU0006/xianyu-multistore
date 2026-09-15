"""
规则模板继承 与 退款决策 测试

模板部分的重点是"被单店覆盖的字段不受模板变更影响" ——
这一条判断错了，影响预览就会虚高，然后没人再看预览。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.refunds import (
    ABUSE_THRESHOLD,
    BuyerHistory,
    Decision,
    RefundReason,
    abuse_score,
    decide,
    is_abusive,
    open_ticket,
    queue_summary,
    render,
)
from backend.templates import (
    DEFAULTS,
    FIELDS,
    RuleSet,
    assert_valid,
    audit_drift,
    diff,
    effective_rules,
    preview_template_change,
    resolve,
    trace,
    unaffected_stores,
    validate,
)

# ===========================================================================
# 一、三层继承
# ===========================================================================

TEMPLATE = RuleSet(bargain_ladder=(0.10, 0.20), low_stock_threshold=20)
OVERRIDE_S1 = RuleSet(bargain_ladder=(0.03, 0.06))     # 只覆盖阶梯
OVERRIDE_S2 = RuleSet()                                 # 完全跟随模板


def test_resolve_merges_layers():
    rules = resolve(DEFAULTS, TEMPLATE, OVERRIDE_S1)
    assert rules.bargain_ladder == (0.03, 0.06)   # 单店胜出
    assert rules.low_stock_threshold == 20        # 模板胜出
    assert rules.max_bargain_rounds == 4          # 默认值兜底


def test_none_means_inherit():
    layer = RuleSet(auto_ship=None, auto_reply=False)
    rules = resolve(RuleSet(auto_ship=True, auto_reply=True), layer)
    assert rules.auto_ship is True                # None 不覆盖
    assert rules.auto_reply is False              # False 是明确的"关掉"


def test_false_is_not_treated_as_missing():
    assert resolve(RuleSet(auto_reply=True), RuleSet(auto_reply=False)).auto_reply is False


def test_later_layer_wins():
    assert resolve(RuleSet(low_stock_threshold=1), RuleSet(low_stock_threshold=2)).low_stock_threshold == 2


def test_trace_reports_the_deciding_layer():
    assert trace("bargain_ladder", DEFAULTS, TEMPLATE, OVERRIDE_S1) == "store"
    assert trace("low_stock_threshold", DEFAULTS, TEMPLATE, OVERRIDE_S1) == "template"
    assert trace("max_bargain_rounds", DEFAULTS, TEMPLATE, OVERRIDE_S1) == "default"


def test_trace_rejects_unknown_field():
    with pytest.raises(KeyError):
        trace("nope", DEFAULTS)


def test_diff_only_reports_real_changes():
    assert diff(TEMPLATE, TEMPLATE) == {}
    changed = diff(TEMPLATE, RuleSet(bargain_ladder=(0.10, 0.20), low_stock_threshold=5))
    assert set(changed) == {"low_stock_threshold"}


def test_dict_roundtrip():
    rules = RuleSet(bargain_ladder=(0.1, 0.2), quiet_hours=(23, 8), auto_ship=True)
    assert RuleSet.from_dict(rules.to_dict()) == rules


# ===========================================================================
# 二、模板变更影响预览
# ===========================================================================

def test_preview_excludes_overridden_fields():
    # 模板改阶梯，但 s1 自己覆盖了阶梯 → s1 不该受影响
    new_template = RuleSet(bargain_ladder=(0.15, 0.30), low_stock_threshold=5)
    impacts = preview_template_change(
        TEMPLATE, new_template, {"s1": OVERRIDE_S1, "s2": OVERRIDE_S2})

    by_store = {i.store_id: i.changed_fields for i in impacts}
    assert "bargain_ladder" not in by_store.get("s1", ())   # 关键断言
    assert "low_stock_threshold" in by_store["s1"]
    assert set(by_store["s2"]) == {"bargain_ladder", "low_stock_threshold"}


def test_preview_returns_nothing_when_template_is_unchanged():
    assert preview_template_change(TEMPLATE, TEMPLATE, {"s1": OVERRIDE_S1}) == ()


def test_unaffected_stores_lists_followers_only():
    # 只改阶梯：s1 自己覆盖了阶梯所以不受影响，s2 跟随模板所以受影响
    new_template = RuleSet(bargain_ladder=(0.50, 0.60), low_stock_threshold=20)
    overrides = {"s1": OVERRIDE_S1, "s2": OVERRIDE_S2}

    assert unaffected_stores(TEMPLATE, new_template, overrides) == ("s1",)
    affected = {i.store_id for i in preview_template_change(TEMPLATE, new_template, overrides)}
    assert affected == {"s2"}


def test_impact_summary_is_readable():
    impacts = preview_template_change(TEMPLATE, RuleSet(low_stock_threshold=5), {"s2": OVERRIDE_S2})
    assert "s2" in impacts[0].summary
    assert "low_stock_threshold" in impacts[0].summary


def test_effective_rules_per_store():
    rules = effective_rules(TEMPLATE, {"s1": OVERRIDE_S1, "s2": OVERRIDE_S2})
    assert rules["s1"].bargain_ladder == (0.03, 0.06)
    assert rules["s2"].bargain_ladder == (0.10, 0.20)


def test_audit_drift_lists_what_each_store_overrides():
    drift = audit_drift(TEMPLATE, {"s1": OVERRIDE_S1, "s2": OVERRIDE_S2})
    assert drift == {"s1": ("bargain_ladder",)}


# ===========================================================================
# 三、规则校验
# ===========================================================================

def test_defaults_are_valid():
    assert validate(DEFAULTS) == []


def test_ladder_must_increase():
    problems = validate(RuleSet(bargain_ladder=(0.20, 0.10)))
    assert any("递增" in p for p in problems)


def test_ladder_range_is_checked():
    assert validate(RuleSet(bargain_ladder=(0.5, 1.5)))
    assert validate(RuleSet(bargain_ladder=()))


def test_negative_threshold_is_rejected():
    assert any("不能为负" in p for p in validate(RuleSet(low_stock_threshold=-1)))


def test_handoff_threshold_range():
    assert validate(RuleSet(handoff_threshold=0))
    assert validate(RuleSet(handoff_threshold=101))
    assert validate(RuleSet(handoff_threshold=60)) == []


def test_quiet_hours_range():
    assert validate(RuleSet(quiet_hours=(24, 8)))
    assert validate(RuleSet(quiet_hours=(23, 8))) == []


def test_assert_valid_raises():
    with pytest.raises(ValueError):
        assert_valid(RuleSet(max_bargain_rounds=0))


def test_all_fields_are_covered_by_defaults():
    resolved = resolve(DEFAULTS)
    assert all(getattr(resolved, f) is not None for f in FIELDS)


# ===========================================================================
# 四、买家画像
# ===========================================================================

def test_clean_buyer_scores_zero():
    assert abuse_score(BuyerHistory(total_orders=20, refunds=0)) == 0


def test_refund_rate_is_capped():
    assert abuse_score(BuyerHistory(total_orders=10, refunds=10)) == 40


def test_small_sample_is_not_penalised():
    # 买 1 单退 1 单不该被算成 100 分
    assert abuse_score(BuyerHistory(total_orders=1, refunds=1)) == 0
    assert abuse_score(BuyerHistory(total_orders=2, refunds=2)) == 0


def test_off_platform_and_complaints_accumulate():
    score = abuse_score(BuyerHistory(total_orders=10, refunds=6,
                                     off_platform_strikes=3, complaints=2))
    assert score == 24 + 30 + 30
    assert is_abusive(score)


def test_score_is_capped_at_100():
    assert abuse_score(BuyerHistory(total_orders=100, refunds=100,
                                    off_platform_strikes=99, complaints=99)) == 100


# ===========================================================================
# 五、退款决策
# ===========================================================================

def test_undelivered_is_always_approved():
    d = decide(RefundReason.NO_LONGER_NEEDED, delivered=False)
    assert d.decision == Decision.AUTO_APPROVE
    assert not d.requires_proof


def test_undelivered_with_unrecognized_reason_still_approved():
    # 未发货是客观事实，与理由文本是否被识别无关 —— 不能因为理由没识别就把
    # 一笔零损失的退款压给人工
    d = decide("买家打了一段自由文本理由", delivered=False)
    assert d.decision == Decision.AUTO_APPROVE


def test_unrecognized_reason_when_delivered_goes_to_review():
    d = decide("买家打了一段自由文本理由", delivered=True)
    assert d.decision == Decision.REVIEW
    assert "无法识别" in d.note


def test_invalid_card_is_approved():
    # 我方责任，不该让人去点按钮
    d = decide(RefundReason.CARD_INVALID, delivered=True, card_revealed=True)
    assert d.decision == Decision.AUTO_APPROVE
    assert "我方责任" in d.note


def test_duplicate_purchase_is_approved():
    d = decide(RefundReason.OTHER, delivered=True, duplicate_order=True)
    assert d.decision == Decision.AUTO_APPROVE


def test_revealed_card_blocks_buyer_fault_refund():
    d = decide(RefundReason.NO_LONGER_NEEDED, delivered=True, card_revealed=True)
    assert d.decision == Decision.AUTO_REJECT


def test_buyer_fault_without_reveal_goes_to_review():
    # 还没看卡密就说不想要了 —— 这种情况有回旋余地，人工看
    d = decide(RefundReason.NO_LONGER_NEEDED, delivered=True, card_revealed=False)
    assert d.decision == Decision.REVIEW


def test_high_risk_buyer_never_auto_rejected():
    history = BuyerHistory(total_orders=10, refunds=8, off_platform_strikes=3)
    d = decide(RefundReason.NO_LONGER_NEEDED, delivered=True, card_revealed=True,
               history=history)
    assert d.decision == Decision.REVIEW      # 高风险也走人工，不自动拒
    assert d.flag_buyer is True


def test_unknown_reason_goes_to_review():
    d = decide("SOMETHING_ELSE", delivered=True)
    assert d.decision == Decision.REVIEW


def test_review_has_a_longer_sla_than_auto_decisions():
    review = decide(RefundReason.OTHER, delivered=True)
    auto = decide(RefundReason.CARD_INVALID, delivered=True)
    assert review.sla_minutes > auto.sla_minutes


def test_render_mentions_decision_and_sla():
    text = render(decide(RefundReason.CARD_INVALID, delivered=True))
    assert "自动同意" in text and "分钟" in text


# ===========================================================================
# 六、售后工单
# ===========================================================================

def _review_ticket(now):
    d = decide(RefundReason.OTHER, delivered=True)
    return open_ticket(order_id="o1", store_id="s1", buyer_id="b1", decision=d, now=now)


def test_auto_handled_refunds_do_not_open_tickets():
    d = decide(RefundReason.CARD_INVALID, delivered=True)
    assert open_ticket(order_id="o1", store_id="s1", buyer_id="b1", decision=d) is None


def test_review_opens_a_ticket():
    now = datetime(2026, 9, 13, 19, 0, tzinfo=timezone.utc)
    assert _review_ticket(now) is not None


def test_ticket_overdue_detection():
    now = datetime(2026, 9, 13, 19, 0, tzinfo=timezone.utc)
    ticket = _review_ticket(now)
    assert not ticket.overdue(now + timedelta(minutes=30))
    assert ticket.overdue(now + timedelta(minutes=121))


def test_queue_summary_flags_overdue():
    now = datetime(2026, 9, 13, 19, 0, tzinfo=timezone.utc)
    tickets = [_review_ticket(now)]
    summary = queue_summary(tickets, now + timedelta(minutes=200))
    assert summary["open"] == 1
    assert summary["overdue"] == 1
    assert summary["needs_attention"] is True


def test_empty_queue():
    now = datetime(2026, 9, 13, 19, 0, tzinfo=timezone.utc)
    assert queue_summary([], now)["open"] == 0
