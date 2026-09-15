"""
运营分析测试

核心断言：漏斗异常能被发现、商品诊断不因小样本乱下结论、健康分扣分方向正确。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.analytics import (
    HealthSnapshot,
    ProductStats,
    STAGE_ORDER,
    Verdict,
    build_funnel,
    build_weekly_report,
    diagnose,
    diagnose_all,
    evaluate_health,
    hourly_heatmap,
    peak_hours,
    quiet_hours,
    render_funnel,
    render_health,
    render_weekly,
)

NOW = datetime(2026, 9, 13, 19, 0, tzinfo=timezone.utc)


# ===========================================================================
# 一、漏斗
# ===========================================================================

def test_rates_are_relative_to_previous_stage():
    f = build_funnel(inquiries=100, bargains=50, orders=20, paid=18, repeats=5)
    rates = f.rates()
    assert rates["bargains"] == 0.5
    assert rates["orders"] == 0.4
    assert rates["paid"] == 0.9
    assert rates["repeats"] == pytest.approx(0.2778, rel=1e-3)
    assert rates["inquiries"] == 1.0


def test_overall_rate():
    f = build_funnel(inquiries=100, paid=25)
    assert f.overall_rate() == 0.25


def test_overall_rate_with_no_inquiries():
    assert build_funnel().overall_rate() == 0.0


def test_zero_division_is_handled():
    f = build_funnel(inquiries=0, bargains=0, orders=5)
    assert f.rates()["orders"] == 0.0


def test_anomalies_detect_impossible_funnel():
    # 成交比咨询还多 —— 数据一定错了
    f = build_funnel(inquiries=10, bargains=10, orders=30)
    problems = f.anomalies()
    assert len(problems) == 1
    assert "下单(30)" in problems[0]


def test_clean_funnel_has_no_anomalies():
    assert build_funnel(inquiries=100, bargains=50, orders=20, paid=20, repeats=5).anomalies() == ()


def test_bottleneck_is_the_leakiest_hop():
    f = build_funnel(inquiries=100, bargains=90, orders=9, paid=9, repeats=5)
    stage, rate = f.bottleneck()
    assert stage == "orders"
    assert rate == pytest.approx(0.1)


def test_build_funnel_rejects_unknown_stage():
    with pytest.raises(KeyError):
        build_funnel(inquiries=1, views=2)


def test_render_funnel_mentions_bottleneck_and_anomaly():
    text = render_funnel(build_funnel(inquiries=10, bargains=10, orders=30))
    assert "最大瓶颈" in text
    assert "数据异常" in text
    assert len(STAGE_ORDER) == 5


# ===========================================================================
# 二、商品诊断
# ===========================================================================

def _stats(**kw):
    base = dict(product_id="p1", title="爱奇艺黄金会员 年卡", stock=50, days_listed=30)
    base.update(kw)
    return ProductStats(**base)


def test_derived_metrics():
    s = _stats(inquiries=100, bargains=40, orders=20, revenue=2000)
    assert s.conversion == 0.2
    assert s.bargain_rate == 0.4
    assert s.avg_price == 100.0


def test_stockout_beats_everything():
    # 缺货优先于其他任何结论 —— 缺货时讨论"曝光不足"是浪费时间
    d = diagnose(_stats(stock=0, orders=5, inquiries=100))
    assert d.verdict == Verdict.STOCKOUT


def test_star_product():
    d = diagnose(_stats(inquiries=20, orders=12, stock=50))
    assert d.verdict == Verdict.STAR


def test_low_traffic_after_enough_days():
    d = diagnose(_stats(inquiries=3, days_listed=30))
    assert d.verdict == Verdict.TRAFFIC


def test_small_sample_on_new_product_is_not_judged():
    # 刚上架两天、5 次咨询 —— 不该下任何结论
    d = diagnose(_stats(inquiries=5, days_listed=2))
    assert d.verdict == Verdict.NORMAL
    assert "样本不足" in d.detail


def test_negotiation_deadlock():
    # 问的人多、都在砍价、就是不成交
    d = diagnose(_stats(inquiries=40, bargains=30, orders=2))
    assert d.verdict == Verdict.NEGOTIATION
    assert "底价" in d.suggestion


def test_pricing_issue():
    d = diagnose(_stats(inquiries=50, bargains=5, orders=1))
    assert d.verdict == Verdict.PRICING


def test_normal_product():
    d = diagnose(_stats(inquiries=50, bargains=10, orders=15, stock=10))
    assert d.verdict == Verdict.STAR   # 转化 30% 且成交够多


def test_diagnose_all_orders_by_urgency():
    items = [
        _stats(product_id="normal", inquiries=50, bargains=10, orders=15),
        _stats(product_id="out", stock=0, orders=3),
        _stats(product_id="pricing", inquiries=50, bargains=5, orders=1),
    ]
    verdicts = [d.verdict for d in diagnose_all(items)]
    assert verdicts[0] == Verdict.STOCKOUT      # 最急
    assert Verdict.PRICING in verdicts


# ===========================================================================
# 三、时段热度
# ===========================================================================

def test_heatmap_uses_fixed_offset():
    # UTC 16:00 → 东八区次日 00:00
    heat = hourly_heatmap([datetime(2026, 9, 13, 16, 0, tzinfo=timezone.utc)])
    assert heat[0] == 1
    assert sum(heat) == 1


def test_heatmap_accepts_naive_timestamps():
    heat = hourly_heatmap([datetime(2026, 9, 13, 12, 0)], tz_offset_hours=0)
    assert heat[12] == 1


def test_heatmap_has_24_buckets():
    assert len(hourly_heatmap([])) == 24


def test_peak_hours():
    heat = [0] * 24
    heat[20], heat[21], heat[12] = 100, 80, 5
    assert peak_hours(heat, top_n=2) == (20, 21)


def test_peak_hours_on_empty_data():
    assert peak_hours([0] * 24) == ()


def test_quiet_hours():
    heat = [0] * 24
    heat[20] = 100
    for h in (0, 1, 2, 3, 4, 5, 6):
        heat[h] = 5          # 低于峰值 20%
    quiet = quiet_hours(heat, ratio=0.2)
    assert 3 in quiet and 20 not in quiet


def test_quiet_hours_on_empty_data_is_all_day():
    assert quiet_hours([0] * 24) == tuple(range(24))


# ===========================================================================
# 四、健康度
# ===========================================================================

def _healthy():
    return HealthSnapshot(faq_hit_rate=0.7, ai_reply_share=0.9, handoff_rate=0.1,
                          p0_open=0, duplicate_rate=0.01, stockout_products=0)


def test_healthy_snapshot_scores_100():
    score, issues = evaluate_health(_healthy())
    assert score == 100
    assert issues == ()


def test_faq_hit_rate_miss_deducts():
    score, issues = evaluate_health(
        HealthSnapshot(faq_hit_rate=0.0, ai_reply_share=0.9, handoff_rate=0.1))
    assert score == 85
    assert any("FAQ 命中率" in i for i in issues)


def test_open_p0_deducts_heavily():
    score, issues = evaluate_health(
        HealthSnapshot(faq_hit_rate=0.7, ai_reply_share=0.9, handoff_rate=0.1, p0_open=3))
    assert score == 70
    assert any("P0" in i for i in issues)


def test_duplicate_rate_miss_deducts():
    score, _ = evaluate_health(
        HealthSnapshot(faq_hit_rate=0.7, ai_reply_share=0.9, handoff_rate=0.1,
                       duplicate_rate=0.30))
    assert score == 75


def test_everything_broken_scores_zero_not_negative():
    score, issues = evaluate_health(HealthSnapshot(
        faq_hit_rate=0.0, ai_reply_share=0.0, handoff_rate=1.0,
        p0_open=5, duplicate_rate=1.0, stockout_products=5))
    assert score == 0
    assert len(issues) == 6


def test_issues_are_sorted_by_weight():
    _, issues = evaluate_health(HealthSnapshot(
        faq_hit_rate=0.0, ai_reply_share=0.9, handoff_rate=0.1,
        p0_open=1, duplicate_rate=0.5))
    # P0（权重 30）应该排在 FAQ 命中率（权重 15）前面
    assert "P0" in issues[0]


def test_render_health():
    assert "健康分 100/100" in render_health(_healthy())
    assert "目标" in render_health(HealthSnapshot(faq_hit_rate=0.0))


# ===========================================================================
# 五、周报
# ===========================================================================

def _report():
    return build_weekly_report(
        period_start=NOW - timedelta(days=7),
        period_end=NOW,
        funnel=build_funnel(inquiries=200, bargains=80, orders=30, paid=28, repeats=6),
        revenue=3584.0,
        ai_cost=1.2,
        health=_healthy(),
        products=[
            ProductStats("p1", "爱奇艺年卡", inquiries=60, orders=25, revenue=3200, stock=100),
            ProductStats("p2", "充电器", inquiries=30, orders=1, revenue=79, stock=0),
        ],
    )


def test_report_profit_and_cost_ratio():
    r = _report()
    assert r.gross_profit == pytest.approx(3582.8)
    assert r.cost_ratio == pytest.approx(0.0003, rel=1e-2)


def test_cost_ratio_with_no_revenue():
    r = build_weekly_report(
        period_start=NOW, period_end=NOW, funnel=build_funnel(),
        revenue=0.0, ai_cost=1.0, health=_healthy())
    assert r.cost_ratio == 0.0


def test_report_picks_top_products_by_revenue():
    assert [p.product_id for p in _report().top_products][0] == "p1"


def test_report_collects_problems_only():
    problems = _report().problems
    assert all(d.verdict != Verdict.NORMAL for d in problems)
    assert any(d.verdict == Verdict.STOCKOUT for d in problems)


def test_render_weekly_contains_everything():
    text = render_weekly(_report())
    assert "周报" in text
    assert "转化漏斗" in text
    assert "健康分" in text
    assert "收入 Top" in text
    assert "需要处理" in text
