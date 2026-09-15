"""
捡漏监控测试

核心断言：便宜的过滤器先跑、贵的多模态只在折扣够大时才调、预算用尽时降级而不是丢弃。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.sourcing import (
    DEFAULT_VISION_BUDGET,
    Reject,
    SourcingResult,
    VISION_COST_PER_CALL,
    Listing,
    MonitorRule,
    VisionVerdict,
    cheap_filter,
    discount_ratio,
    estimate_daily_vision_cost,
    is_stale,
    keyword_hit,
    needs_vision,
    plan_filtering,
    price_in_range,
    render_result,
    run_sourcing,
)

run = asyncio.run
NOW = datetime(2026, 9, 13, 19, 0, tzinfo=timezone.utc)
MARKET = 1500.0


def rule(**kw):
    base = dict(keyword="Switch OLED", min_price=1000, max_price=1800,
                min_desire=5, vision_discount_threshold=0.15)
    base.update(kw)
    return MonitorRule(**base)


def listing(item_id="i1", title="Switch OLED 日版 9成新", price=1200.0,
            desire=10, description="", listed_at=None):
    return Listing(item_id=item_id, title=title, price=price, desire_count=desire,
                   description=description, listed_at=listed_at)


class FakeVerifier:
    def __init__(self, risky_ids=()):
        self.risky = set(risky_ids)
        self.calls: list[str] = []

    async def verify(self, item: Listing) -> VisionVerdict:
        self.calls.append(item.item_id)
        if item.item_id in self.risky:
            return VisionVerdict(True, ("疑似屏幕划痕",), 0.8)
        return VisionVerdict(False, (), 0.9)


# ===========================================================================
# 一、廉价过滤
# ===========================================================================

def test_discount_ratio():
    assert discount_ratio(1200.0, 1500.0) == 0.2
    assert discount_ratio(1500.0, 1500.0) == 0.0
    assert discount_ratio(1800.0, 1500.0) < 0


def test_discount_ratio_guards_against_zero_market_price():
    assert discount_ratio(100.0, 0.0) == 0.0


def test_keyword_hit_on_title_or_description():
    assert keyword_hit(listing(), "Switch OLED")
    assert keyword_hit(listing(title="随便", description="这是 Switch OLED"), "Switch OLED")
    assert not keyword_hit(listing(title="PS5 手柄"), "Switch OLED")


def test_keyword_hit_is_case_insensitive():
    assert keyword_hit(listing(title="switch oled 日版"), "Switch OLED")


def test_empty_keyword_matches_everything():
    assert keyword_hit(listing(title="随便"), "")


def test_price_in_range():
    r = rule()
    assert price_in_range(1200, r)
    assert not price_in_range(900, r)
    assert not price_in_range(2000, r)


def test_open_ended_price_range():
    r = rule(min_price=None, max_price=None)
    assert price_in_range(99999, r)


def test_stale_detection():
    r = rule(max_age_hours=24)
    fresh = listing(listed_at=NOW - timedelta(hours=2))
    old = listing(listed_at=NOW - timedelta(hours=48))
    assert not is_stale(fresh, r, NOW)
    assert is_stale(old, r, NOW)


def test_missing_listed_at_is_not_stale():
    assert not is_stale(listing(listed_at=None), rule(), NOW)


def test_cheap_filter_returns_reject_reasons():
    r = rule()
    assert cheap_filter(listing(), r, now=NOW) is None
    assert cheap_filter(listing(item_id="x", title="PS5"), r, now=NOW).reason == Reject.KEYWORD
    assert cheap_filter(listing(item_id="x", price=3000), r, now=NOW).reason == Reject.PRICE
    assert cheap_filter(listing(item_id="x", desire=1), r, now=NOW).reason == Reject.DESIRE
    assert cheap_filter(listing(item_id="x"), r, seen_item_ids=frozenset({"x"}),
                        now=NOW).reason == Reject.DUPLICATE


def test_stale_rejection_comes_last():
    r = rule(max_age_hours=1)
    old = listing(item_id="x", price=3000, listed_at=NOW - timedelta(hours=5))
    # 价格和过期都不合格时，先报价格 —— 廉价判断优先，省得白算时间
    assert cheap_filter(old, r, now=NOW).reason == Reject.PRICE


# ===========================================================================
# 二、多模态门槛（成本控制的核心）
# ===========================================================================

def test_vision_only_for_deep_discounts():
    r = rule(vision_discount_threshold=0.15)
    assert needs_vision(listing(price=1200.0), r, MARKET)        # 便宜 20%
    assert not needs_vision(listing(price=1450.0), r, MARKET)    # 只便宜 3.3%


def test_threshold_boundary_is_inclusive():
    r = rule(vision_discount_threshold=0.20)
    assert needs_vision(listing(price=1200.0), r, MARKET)        # 正好 20%


def test_expensive_item_never_triggers_vision():
    r = rule(vision_discount_threshold=0.15)
    assert not needs_vision(listing(price=2000.0), r, MARKET)


# ===========================================================================
# 三、过滤计划
# ===========================================================================

def test_plan_splits_listings_three_ways():
    listings = [
        listing(item_id="pass", price=1200.0),      # 折扣够 → 需要鉴真
        listing(item_id="cheap-pass", price=1450.0),  # 通过过滤但折扣不够
        listing(item_id="rej", price=3000.0),       # 价格超区间
    ]
    plan = plan_filtering(listings, rule(), MARKET, now=NOW)

    assert [i.item_id for i in plan.to_verify] == ["pass"]
    assert [i.item_id for i in plan.no_vision_needed] == ["cheap-pass"]
    assert [r.listing.item_id for r in plan.rejected] == ["rej"]


def test_plan_respects_vision_budget():
    listings = [listing(item_id=f"i{i}", price=1200.0) for i in range(10)]
    plan = plan_filtering(listings, rule(), MARKET, vision_budget=3, now=NOW)

    assert len(plan.to_verify) == 3
    assert plan.budget_exhausted is True
    assert len(plan.no_vision_needed) == 7      # 超预算的降级，不是丢弃


def test_plan_with_zero_budget_skips_all_vision():
    listings = [listing(item_id="a", price=1200.0)]
    plan = plan_filtering(listings, rule(), MARKET, vision_budget=0, now=NOW)
    assert plan.to_verify == ()
    assert plan.vision_skipped == 1
    assert plan.budget_exhausted is True


def test_plan_ignores_duplicates():
    listings = [listing(item_id="seen")]
    plan = plan_filtering(listings, rule(), MARKET, seen_item_ids=frozenset({"seen"}), now=NOW)
    assert plan.rejected[0].reason == Reject.DUPLICATE


def test_plan_is_pure_and_repeatable():
    listings = [listing(item_id="a", price=1200.0)]
    first = plan_filtering(listings, rule(), MARKET, now=NOW)
    second = plan_filtering(listings, rule(), MARKET, now=NOW)
    assert first.to_verify == second.to_verify


# ===========================================================================
# 四、执行
# ===========================================================================

def test_run_sourcing_calls_vision_only_for_planned_items():
    async def scenario():
        verifier = FakeVerifier()
        listings = [
            listing(item_id="deep", price=1200.0),
            listing(item_id="shallow", price=1450.0),
        ]
        result = await run_sourcing(listings, rule(), MARKET, verifier, now=NOW)
        return result, verifier

    result, verifier = run(scenario())
    assert verifier.calls == ["deep"]           # shallow 没花钱
    assert result.vision_calls == 1
    assert result.vision_cost == pytest.approx(VISION_COST_PER_CALL)
    assert {i.item_id for i in result.accepted} == {"deep", "shallow"}


def test_risky_item_is_rejected_with_labels():
    async def scenario():
        verifier = FakeVerifier(risky_ids={"bad"})
        result = await run_sourcing([listing(item_id="bad", price=1200.0)],
                                    rule(), MARKET, verifier, now=NOW)
        return result

    result = run(scenario())
    assert result.accepted == ()
    assert result.rejected[0].reason == Reject.VISION_RISKY
    assert "划痕" in result.rejected[0].detail


def test_run_sourcing_without_verifier_still_works():
    async def scenario():
        return await run_sourcing([listing(item_id="a", price=1200.0)], rule(), MARKET, now=NOW)

    result = run(scenario())
    # 没有鉴真实现时，本来该鉴真的商品也直接放行，而不是丢掉
    assert len(result.accepted) == 1
    assert result.vision_calls == 0


def test_result_breakdown_and_hit_rate():
    async def scenario():
        verifier = FakeVerifier(risky_ids={"bad"})
        listings = [listing(item_id="bad", price=1200.0), listing(item_id="ok", price=1200.0),
                    listing(item_id="rej", price=3000.0)]
        return await run_sourcing(listings, rule(), MARKET, verifier, now=NOW)

    result = run(scenario())
    assert result.reject_breakdown == {Reject.VISION_RISKY: 1, Reject.PRICE: 1}
    assert result.vision_hit_rate == 0.5        # 2 次鉴真里 1 次判风险
    assert result.scanned == 3


def test_hit_rate_with_no_calls():
    assert SourcingResult((), (), 0, 0.0, 0).vision_hit_rate == 0.0


def test_daily_cost_estimate():
    assert estimate_daily_vision_cost(0) == 0.0
    assert estimate_daily_vision_cost(50) == pytest.approx(1.0)


def test_render_result_mentions_cost_and_budget():
    async def scenario():
        listings = [listing(item_id=f"i{i}", price=1200.0) for i in range(5)]
        return await run_sourcing(listings, rule(), MARKET, FakeVerifier(),
                                  vision_budget=2, now=NOW)

    text = render_result(run(scenario()))
    assert "多模态调用 2 次" in text
    assert "跳过 3 次" in text
    assert "日预算已用尽" in text


def test_default_budget_is_conservative():
    assert DEFAULT_VISION_BUDGET <= 100
