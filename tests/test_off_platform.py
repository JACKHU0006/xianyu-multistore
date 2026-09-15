"""
站外引流拦截测试

重点：买家会用变体绕过，所以每条"硬命中"都要有对应的变体测试。
另外误报也要测 —— 把"QQ音乐"当成引流拦掉，比漏拦还伤体验。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.off_platform_guard import (
    Action,
    BuyerRisk,
    RiskLevel,
    effective_strikes,
    normalize_digits,
    normalize_light,
    observe,
    record_strike,
    risk_level,
    safe_reply,
    scan,
    should_flag_orders,
    strip_symbols,
)

NOW = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)


# ===========================================================================
# 一、归一化对抗
# ===========================================================================

def test_zero_width_chars_are_removed():
    assert normalize_light("微\u200b信") == "微信"
    assert normalize_light("v\ufeffx") == "vx"


def test_fullwidth_is_folded():
    assert normalize_light("ｖｘ") == "vx"
    assert normalize_light("１３８") == "138"


def test_case_is_folded():
    assert normalize_light("WX") == "wx"


def test_chinese_digits_convert():
    assert normalize_digits("一三八零零一三八零零零") == "13800138000"
    assert normalize_digits("两") == "2"


def test_strip_symbols_joins_fragments():
    assert strip_symbols("1-3-8 0013_8000") == "13800138000"
    assert strip_symbols("v 信") == "v信"


# ===========================================================================
# 二、手机号：各种写法都要抓到
# ===========================================================================

def test_plain_phone_is_blocked():
    r = scan("加我 13800138000")
    assert r.blocked
    assert "PHONE" in r.rules


@pytest.mark.parametrize("raw", [
    "138-0013-8000",
    "138 0013 8000",
    "138.0013.8000",
    "13800138000",
    "一三八零零一三八零零零",
    "１３８００１３８０００",
    "1 3 8 0 0 1 3 8 0 0 0",
])
def test_phone_variants_are_blocked(raw):
    assert scan(f"我的号是 {raw}").blocked, raw


def test_long_order_number_is_not_a_phone():
    # 15 位订单号里含 11 位数字片段，不能被误判成手机号
    assert scan("订单号 138001380001234 什么时候到").clean


def test_ten_digit_number_is_not_a_phone():
    assert scan("编号 1380013800").clean


# ===========================================================================
# 三、链接
# ===========================================================================

@pytest.mark.parametrize("raw", [
    "https://weixin.qq.com/x",
    "http://abc.com",
    "www.taobao.com",
    "看这个 abc.xyz",
])
def test_links_are_blocked(raw):
    assert scan(raw).blocked, raw


# ===========================================================================
# 四、联系方式关键词
# ===========================================================================

def test_contact_keyword_with_identifier_is_blocked():
    r = scan("微信 abc123")
    assert r.blocked
    assert "CONTACT_WITH_ID" in r.rules


def test_bare_contact_keyword_only_warns():
    r = scan("咱们可以微信聊吗")
    assert r.action == Action.WARN


def test_variant_keywords_are_recognized():
    for raw in ["加v信", "薇信联系", "威信多少", "加 vx", "WX 号"]:
        assert scan(raw).action != Action.ALLOW, raw


def test_multiple_weak_signals_escalate_to_block():
    # "加我" + "微信" 单独都是 WARN，合起来意图已经明确
    r = scan("加我微信")
    assert r.blocked
    assert "COMBINED" in r.rules


def test_qq_with_account_is_blocked():
    r = scan("扣扣 12345678")
    assert r.blocked


def test_benign_product_name_is_not_flagged():
    assert scan("QQ音乐会员有吗").clean
    assert scan("微信读书年卡多少钱").clean


# ===========================================================================
# 五、站外支付
# ===========================================================================

def test_payment_keyword_alone_warns():
    assert scan("能不能转账").action == Action.WARN


def test_payment_with_account_is_blocked():
    r = scan("支付宝 13800138000 转我")
    assert r.blocked


# ===========================================================================
# 六、正常消息不该被拦
# ===========================================================================

@pytest.mark.parametrize("text", [
    "这个多久发货呀",
    "支持七天无理由吗",
    "能便宜点吗，我买两张",
    "拍下了，什么时候发卡密",
    "订单号 720188445521 物流到哪了",
    "",
    "   ",
])
def test_normal_messages_pass(text):
    assert scan(text).clean, text


# ===========================================================================
# 七、话术与风险累计
# ===========================================================================

def test_safe_reply_only_for_non_allow():
    assert safe_reply(scan("这个多少钱")) is None
    assert safe_reply(scan("加我微信")) is not None


def test_deflect_reply_does_not_echo_the_contact():
    text = safe_reply(scan("我微信是 abc123"))
    assert "abc123" not in text
    assert "微信" not in text.replace("站外", "")


def test_risk_levels():
    assert risk_level(0) == RiskLevel.NORMAL
    assert risk_level(1) == RiskLevel.WATCH
    assert risk_level(3) == RiskLevel.HIGH


def test_record_strike_escalates_level():
    risk = BuyerRisk(buyer_id="b1")
    record_strike(risk, NOW)
    assert risk.level == RiskLevel.WATCH
    record_strike(risk, NOW)
    record_strike(risk, NOW)
    assert risk.level == RiskLevel.HIGH
    assert should_flag_orders(risk) is True


def test_observe_only_counts_blocks():
    risk = BuyerRisk(buyer_id="b1")
    observe(risk, scan("这个多久发货"), NOW)      # 正常消息
    assert risk.strikes == 0
    observe(risk, scan("加我微信"), NOW)          # 命中
    assert risk.strikes == 1


def test_strikes_decay_after_window():
    risk = BuyerRisk(buyer_id="b1")
    record_strike(risk, NOW)
    record_strike(risk, NOW)
    record_strike(risk, NOW)
    assert effective_strikes(risk, NOW) == 3

    later = NOW + timedelta(days=31)
    assert effective_strikes(risk, later) == 0     # 半年前问过一次不该永久拉黑


def test_strike_after_decay_restarts_count():
    risk = BuyerRisk(buyer_id="b1")
    record_strike(risk, NOW)
    record_strike(risk, NOW)
    record_strike(risk, NOW)

    later = NOW + timedelta(days=31)
    record_strike(risk, later)
    assert risk.strikes == 1
    assert risk.level == RiskLevel.WATCH
