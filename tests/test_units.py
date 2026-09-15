"""
纯逻辑单元测试 —— 覆盖两块最容易出钱命的地方：议价边界 与 库存预警分级。

运行：  pytest -q
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import ValidationError

from backend.guardrails import (
    FALLBACK_REPLY,
    AiDecision,
    clamp_offer,
    offer_band,
)
from backend.idempotency import (
    DEFAULT_TTL,
    FINGERPRINT_TTL,
    FailOpenStore,
    IncomingMessage,
    MemoryIdempotencyStore,
    dedup_key,
    filter_new,
    fingerprint,
)
from backend.inventory import (
    STOCK_LOW,
    STOCK_OK,
    STOCK_OUT,
    InventoryAlert,
    alert_priority,
    burn_rate,
    days_of_cover,
    format_cover,
    render_alert_text,
    stock_level,
)

run = asyncio.run


# ===========================================================================
# 一、议价边界
# ===========================================================================

LADDER = [0.05, 0.12, 0.23]  # 逐轮最大让步比例


def band(round_no, listed=100.0, minimum=80.0, ladder=LADDER):
    return offer_band(listed_price=listed, min_price=minimum, ladder=ladder, round_no=round_no)


def test_band_shrinks_each_round():
    assert (band(1).low, band(1).high) == (95.0, 100.0)
    assert (band(2).low, band(2).high) == (88.0, 100.0)
    assert (band(3).low, band(3).high) == (80.0, 100.0)  # 77 被底价 80 兜住


def test_band_beyond_ladder_uses_last_step():
    assert band(9).low == 80.0


def test_band_never_pierces_floor():
    # 极端阶梯：让 50%，但底价 80 必须兜住
    b = offer_band(listed_price=100.0, min_price=80.0, ladder=[0.5], round_no=1)
    assert b.low == 80.0


def test_band_without_ladder_means_no_discount():
    b = offer_band(listed_price=100.0, min_price=80.0, ladder=[], round_no=1)
    assert (b.low, b.high) == (100.0, 100.0)


def test_band_rejects_broken_config():
    with pytest.raises(ValueError):
        offer_band(listed_price=100.0, min_price=120.0, ladder=LADDER, round_no=1)


def test_offer_inside_band_passes():
    assert clamp_offer(listed_price=100.0, min_price=80.0, ladder=LADDER,
                       round_no=1, ai_offered=97.0) == 97.0
    assert clamp_offer(listed_price=100.0, min_price=80.0, ladder=LADDER,
                       round_no=3, ai_offered=80.0) == 80.0


def test_offer_below_this_round_floor_is_rejected():
    # 第 1 轮下限 95，AI 报 90 —— 不能夹成 95，必须整条重来
    assert clamp_offer(listed_price=100.0, min_price=80.0, ladder=LADDER,
                       round_no=1, ai_offered=90.0) is None


def test_offer_below_hard_floor_is_rejected():
    assert clamp_offer(listed_price=100.0, min_price=80.0, ladder=LADDER,
                       round_no=3, ai_offered=79.99) is None


def test_offer_above_listed_is_rejected():
    assert clamp_offer(listed_price=100.0, min_price=80.0, ladder=LADDER,
                       round_no=1, ai_offered=105.0) is None


def test_missing_offer_is_rejected():
    assert clamp_offer(listed_price=100.0, min_price=80.0, ladder=LADDER,
                       round_no=1, ai_offered=None) is None


def test_ai_decision_blocks_off_platform_contact():
    for bad in ["加我微信详聊", "vx 是 abc123", "https://taobao.com/x"]:
        with pytest.raises(ValidationError):
            AiDecision(reply=bad, intent="ENQUIRY")


def test_ai_decision_accepts_clean_reply():
    d = AiDecision(reply="您好，拍下后 5 秒内自动发卡密～", offered_price=None, intent="ENQUIRY")
    assert d.intent == "ENQUIRY"


def test_ai_decision_rejects_unknown_intent():
    with pytest.raises(ValidationError):
        AiDecision(reply="好的", intent="HAGGLE")


def test_fallback_reply_carries_no_price():
    assert "¥" not in FALLBACK_REPLY and "元" not in FALLBACK_REPLY


# ===========================================================================
# 二、消息幂等
# ===========================================================================

def test_fingerprint_separator_prevents_collision():
    assert fingerprint("ab", "c") != fingerprint("a", "bc")


def test_dedup_key_prefers_platform_id():
    k1, ttl1 = dedup_key("s1", msg_id="m-100", buyer_id="b1", content="在吗")
    k2, ttl2 = dedup_key("s1", msg_id="m-100", buyer_id="b1", content="在吗")
    assert k1 == k2 and ttl1 == DEFAULT_TTL == ttl2


def test_dedup_key_is_store_scoped():
    k1, _ = dedup_key("s1", msg_id="m-100")
    k2, _ = dedup_key("s2", msg_id="m-100")
    assert k1 != k2


def test_dedup_key_falls_back_to_fingerprint_with_short_ttl():
    _, ttl = dedup_key("s1", msg_id=None, buyer_id="b1", content="便宜点", sent_at=1_700_000_000)
    assert ttl == FINGERPRINT_TTL


def test_fingerprint_key_stable_across_ms_jitter():
    a, _ = dedup_key("s1", buyer_id="b1", content="便宜点", sent_at=1_700_000_000_000)
    b, _ = dedup_key("s1", buyer_id="b1", content="便宜点", sent_at=1_700_000_000_400)
    assert a == b


def test_memory_store_claims_once():
    store = MemoryIdempotencyStore()
    assert run(store.claim("k", 60)) is True
    assert run(store.claim("k", 60)) is False


def test_memory_store_reclaims_after_expiry():
    store = MemoryIdempotencyStore()
    run(store.claim("k", 60))
    store._seen["k"] = time.monotonic() - 1  # 模拟过期
    assert run(store.claim("k", 60)) is True


def test_sweep_removes_expired_entries():
    store = MemoryIdempotencyStore()
    run(store.claim("alive", 60))
    run(store.claim("dead", 60))
    store._seen["dead"] = time.monotonic() - 1
    assert store.sweep() == 1
    assert "alive" in store._seen and "dead" not in store._seen


def test_filter_new_drops_only_duplicates():
    store = MemoryIdempotencyStore()
    msgs = [
        IncomingMessage(buyer_id="b1", content="在吗", msg_id="m1"),
        IncomingMessage(buyer_id="b1", content="便宜点", msg_id="m2"),
        IncomingMessage(buyer_id="b1", content="便宜点", msg_id="m2"),  # 重投
        IncomingMessage(buyer_id="b2", content="有货吗", msg_id="m3"),
    ]
    out = run(filter_new("s1", msgs, store))
    assert [m.msg_id for m in out.fresh] == ["m1", "m2", "m3"]
    assert [m.msg_id for m in out.duplicates] == ["m2"]
    assert out.duplicate_rate == pytest.approx(0.25)


def test_filter_new_second_pass_is_all_duplicates():
    store = MemoryIdempotencyStore()
    msgs = [IncomingMessage(buyer_id="b1", content="hi", msg_id="m1")]
    assert len(run(filter_new("s1", msgs, store)).fresh) == 1
    assert len(run(filter_new("s1", msgs, store)).fresh) == 0


def test_fail_open_store_never_blocks_on_error():
    class Broken:
        async def claim(self, key, ttl):
            raise ConnectionError("redis down")

    seen = []
    store = FailOpenStore(Broken(), on_error=lambda exc, key: seen.append(key))
    assert run(store.claim("k", 60)) is True  # 放行，不能卡死
    assert seen == ["k"]


def test_fail_open_store_passes_through_when_healthy():
    store = FailOpenStore(MemoryIdempotencyStore())
    assert run(store.claim("k", 60)) is True
    assert run(store.claim("k", 60)) is False


# ===========================================================================
# 三、库存分级
# ===========================================================================

def test_stock_level_boundaries():
    assert stock_level(0, 10) == STOCK_OUT
    assert stock_level(1, 10) == STOCK_LOW
    assert stock_level(10, 10) == STOCK_LOW   # 等于阈值算预警
    assert stock_level(11, 10) == STOCK_OK


def test_burn_rate_averages_over_window():
    assert burn_rate(14, 7) == 2.0
    assert burn_rate(0, 7) == 0.0


def test_burn_rate_rejects_bad_window():
    with pytest.raises(ValueError):
        burn_rate(10, 0)


def test_days_of_cover():
    assert days_of_cover(10, 2.0) == 5.0
    assert days_of_cover(10, 0.0) is None  # 还没开始卖，不报紧急


def test_priority_out_of_stock_is_p0():
    assert alert_priority(STOCK_OUT, None) == "P0"


def test_priority_low_but_dying_today_is_p0():
    # 关键规则：库存不为零，但今天就要用完 —— 和最危险的断货同级
    assert alert_priority(STOCK_LOW, 0.4) == "P0"


def test_priority_normal_level_can_still_be_p0():
    # 可用 11 > 阈值 10，但日均消耗 20，今天就会卖空 —— 这就是"静默卖超"
    assert alert_priority(STOCK_OK, 0.55) == "P0"


def test_priority_low_with_runway_is_p1():
    assert alert_priority(STOCK_LOW, 3.0) == "P1"


def test_priority_healthy_is_p2():
    assert alert_priority(STOCK_OK, 30.0) == "P2"


def test_format_cover():
    assert format_cover(None) == "暂无消耗数据"
    assert format_cover(0.5) == "不足 1 天"
    assert format_cover(4.0) == "约 4.0 天"


def _alert(level, available, cover, paused=False):
    return InventoryAlert(
        product_id="p1", store_id="s1", title="爱奇艺黄金会员 年卡", level=level,
        available=available, threshold=10, daily_burn=2.0, cover_days=cover,
        priority=alert_priority(level, cover),
        should_pause=(level == STOCK_OUT), paused_now=paused,
    )


def test_render_alert_marks_auto_paused():
    text = render_alert_text(_alert(STOCK_OUT, 0, None, paused=True), "会员卡券铺")
    assert text.startswith("【紧急】")
    assert "已自动下架" in text


def test_render_alert_shows_runway():
    text = render_alert_text(_alert(STOCK_LOW, 8, 4.0), "会员卡券铺")
    assert "剩余 8 条" in text and "约 4.0 天" in text


def test_render_alert_asks_for_manual_pause_when_auto_disabled():
    text = render_alert_text(_alert(STOCK_OUT, 0, None, paused=False))
    assert "需尽快下架" in text
