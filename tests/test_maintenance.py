"""
维护任务测试

核心断言：留存策略按表区分、容量估算的阈值判断正确、
重复率能被解读成"刚断过线"这样的可行动结论。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.idempotency import (
    IncomingMessage,
    MemoryIdempotencyStore,
    filter_new_batched,
)
from backend.maintenance import (
    ALARMING_THRESHOLD,
    DedupLevel,
    DedupStats,
    RETENTION_DAYS,
    archive_cutoff,
    build_maintenance_report,
    dedup_stats,
    dedup_stats_from_counts,
    estimate_quota_pressure,
    estimate_table_mb,
    plan_retention,
    render_dedup,
    render_retention_plan,
    should_archive,
)

run = asyncio.run
NOW = datetime(2026, 9, 13, 19, 0, tzinfo=timezone.utc)


# ===========================================================================
# 一、留存策略
# ===========================================================================

def test_archive_cutoff():
    assert archive_cutoff(NOW, 90) == NOW - timedelta(days=90)


def test_archive_cutoff_rejects_bad_input():
    with pytest.raises(ValueError):
        archive_cutoff(NOW, 0)


def test_should_archive():
    assert should_archive(NOW - timedelta(days=100), NOW, 90)
    assert not should_archive(NOW - timedelta(days=10), NOW, 90)


def test_should_archive_accepts_naive_timestamps():
    naive = (NOW - timedelta(days=100)).replace(tzinfo=None)
    assert should_archive(naive, NOW, 90)


def test_plan_covers_permanent_and_unknown_tables():
    plans = {p.table: p for p in plan_retention(
        NOW, {"message_log": 1000, "orders": 500, "weird_table": 10})}

    assert plans["message_log"].actionable
    assert plans["message_log"].cutoff == NOW - timedelta(days=90)
    assert not plans["orders"].actionable            # 永久保留
    assert "永久保留" in plans["orders"].reason
    assert not plans["weird_table"].actionable       # 未配置，不擅自删
    assert "未配置" in plans["weird_table"].reason


def test_retention_policy_keeps_audit_longer_than_messages():
    # 审计要留两年，会话 90 天就够 —— 这两者不该一样
    assert RETENTION_DAYS["audit_log"] > RETENTION_DAYS["message_log"]
    assert RETENTION_DAYS["orders"] is None


def test_plan_can_override_policy():
    plans = plan_retention(NOW, {"message_log": 10}, retention={"message_log": 30})
    assert plans[0].cutoff == NOW - timedelta(days=30)


def test_render_retention_plan():
    text = render_retention_plan(plan_retention(NOW, {"message_log": 10, "orders": 5}))
    assert "message_log" in text and "orders" in text


# ===========================================================================
# 二、容量估算
# ===========================================================================

def test_estimate_table_mb():
    assert estimate_table_mb(0) == 0.0
    # message_log 每行 512 字节 → 2048 行正好 1MB
    assert estimate_table_mb(2048) == pytest.approx(1.0, rel=1e-3)
    # 审计行更大
    assert estimate_table_mb(1000, "audit_log") > estimate_table_mb(1000, "message_log")


def test_quota_pressure_small_is_fine():
    result = estimate_quota_pressure({"message_log": 10_000})
    assert result["used_mb"] == pytest.approx(4.88, rel=1e-2)
    assert result["should_act"] is False


def test_quota_pressure_crosses_threshold_at_70_percent():
    # 到 70% 就该动手，不要等 95% —— 真实占用还含索引膨胀
    result = estimate_quota_pressure({"message_log": 750_000})
    assert result["used_ratio"] >= 0.7
    assert result["should_act"] is True


def test_quota_pressure_reports_per_table():
    result = estimate_quota_pressure({"message_log": 1000, "audit_log": 1000})
    assert set(result["by_table"]) == {"message_log", "audit_log"}


def test_quota_pressure_with_zero_quota():
    assert estimate_quota_pressure({"message_log": 10}, quota_mb=0)["should_act"] is False


# ===========================================================================
# 三、重复率监控
# ===========================================================================

def test_duplicate_rate():
    assert DedupStats(total=200, duplicates=4).duplicate_rate == 0.02
    assert DedupStats().duplicate_rate == 0.0


def test_levels():
    assert DedupStats(200, 2).level == DedupLevel.NORMAL
    assert DedupStats(200, 20).level == DedupLevel.ELEVATED
    assert DedupStats(200, 50).level == DedupLevel.ALARMING


def test_thresholds_are_boundary_inclusive():
    assert dedup_stats_from_counts(100, 5).level == DedupLevel.ELEVATED
    assert dedup_stats_from_counts(100, int(100 * ALARMING_THRESHOLD)).level == DedupLevel.ALARMING


def test_alarming_diagnosis_points_at_connection():
    # 这是这个指标最有价值的地方：它是免费的断线探测器
    text = DedupStats(100, 30).diagnosis()
    assert "断线重连" in text or "补推" in text
    assert DedupStats(100, 30).needs_attention is True


def test_normal_diagnosis_is_quiet():
    stats = DedupStats(1000, 10)
    assert "正常" in stats.diagnosis()
    assert stats.needs_attention is False


def test_render_dedup():
    text = render_dedup(DedupStats(100, 30))
    assert "重复 30 条" in text and "30.0%" in text


def test_dedup_stats_aggregates_outcomes():
    store = MemoryIdempotencyStore()
    msgs = [
        IncomingMessage(buyer_id="b1", content="a", msg_id="m1"),
        IncomingMessage(buyer_id="b1", content="b", msg_id="m2"),
    ]
    first = run(filter_new_batched("s1", msgs, store))
    second = run(filter_new_batched("s1", msgs, store))

    stats = dedup_stats([first, second])
    assert stats.total == 4
    assert stats.duplicates == 2
    assert stats.duplicate_rate == 0.5
    assert stats.level == DedupLevel.ALARMING     # 第二次全部是重复


# ===========================================================================
# 四、批量去重（优化）
# ===========================================================================

def test_batch_dedup_preserves_order():
    store = MemoryIdempotencyStore()
    msgs = [IncomingMessage(buyer_id="b1", content=f"m{i}", msg_id=f"id{i}") for i in range(5)]
    outcome = run(filter_new_batched("s1", msgs, store))
    assert [m.msg_id for m in outcome.fresh] == ["id0", "id1", "id2", "id3", "id4"]


def test_batch_dedup_marks_repeats_inside_the_batch():
    store = MemoryIdempotencyStore()
    msgs = [
        IncomingMessage(buyer_id="b1", content="x", msg_id="dup"),
        IncomingMessage(buyer_id="b1", content="y", msg_id="other"),
        IncomingMessage(buyer_id="b1", content="x", msg_id="dup"),
    ]
    outcome = run(filter_new_batched("s1", msgs, store))
    assert [m.msg_id for m in outcome.fresh] == ["dup", "other"]
    assert [m.msg_id for m in outcome.duplicates] == ["dup"]


def test_batch_dedup_handles_empty_input():
    outcome = run(filter_new_batched("s1", [], MemoryIdempotencyStore()))
    assert outcome.total == 0


def test_batch_dedup_matches_single_version():
    msgs = [IncomingMessage(buyer_id="b1", content=f"m{i}", msg_id=f"id{i}") for i in range(4)]

    batch_store = MemoryIdempotencyStore()
    batch = run(filter_new_batched("s1", msgs, batch_store))

    from backend.idempotency import filter_new

    single_store = MemoryIdempotencyStore()
    single = run(filter_new("s1", msgs, single_store))

    assert [m.msg_id for m in batch.fresh] == [m.msg_id for m in single.fresh]


# ===========================================================================
# 五、汇总报告
# ===========================================================================

def test_maintenance_report_combines_everything():
    report = build_maintenance_report(
        NOW,
        {"message_log": 750_000, "orders": 100},
        DedupStats(1000, 200),
    )
    assert report["needs_attention"] is True
    assert "留存计划" in report["text"]
    assert "容量" in report["text"]
    assert "消息去重" in report["text"]


def test_maintenance_report_is_quiet_when_all_good():
    report = build_maintenance_report(
        NOW, {"message_log": 1000}, DedupStats(1000, 5))
    assert report["needs_attention"] is False
