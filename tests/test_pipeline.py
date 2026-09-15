"""
消息处理管线测试

重点：
  - 三个优化点真的生效（FAQ 命中不调模型 / 上下文被压缩 / 成本被记账）
  - 每个提前返回的分支都不该产生模型调用
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from backend.guardrails import AiDecision, FALLBACK_REPLY
from backend.handoff import ConvState, Reason
from backend.idempotency import IncomingMessage, MemoryIdempotencyStore
from backend.off_platform_guard import DEFLECT_BLOCK
from backend.pipeline import (
    Action,
    Faq,
    HANDOFF_HOLD_REPLY,
    LlmOutcome,
    ProductContext,
    Turn,
    UsageTracker,
    build_system_prompt,
    compress_context,
    digest_turns,
    estimate_cost,
    handle_turn,
    match_faq,
    tokenize,
)

run = asyncio.run
NOW = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)

PRODUCT = ProductContext(
    title="爱奇艺黄金会员 年卡",
    listed_price=128.0,
    min_price=99.0,
    ladder=(0.05, 0.12, 0.23),
    shipping_policy="虚拟发货，不支持退换",
)

FAQS = [
    Faq("多久发货", "支付成功后 5 秒内自动发卡密，注意查收私信。"),
    Faq("支持哪些设备", "手机、平板、电视端都支持，一个账号通用。"),
]


class FakeLlm:
    def __init__(self, decision: AiDecision, *, prompt_tokens=1000, completion_tokens=200,
                 confidence=0.9):
        self._outcome = LlmOutcome(decision, prompt_tokens, completion_tokens, confidence)
        self.calls = 0
        self.last_kwargs: dict = {}

    async def decide(self, **kwargs) -> LlmOutcome:
        self.calls += 1
        self.last_kwargs = kwargs
        return self._outcome


def llm_returning(reply="好的亲～", offered_price=None, intent="ENQUIRY", **kw):
    return FakeLlm(AiDecision(reply=reply, offered_price=offered_price, intent=intent), **kw)


def msg(content, msg_id="m1", buyer="b1"):
    return IncomingMessage(buyer_id=buyer, content=content, msg_id=msg_id, sent_at=NOW)


# ===========================================================================
# 一、FAQ 匹配（优化一）
# ===========================================================================

def test_tokenize_splits_cjk_into_bigrams():
    assert "发货" in tokenize("多久发货")
    assert "微信" in tokenize("微信联系")


def test_faq_exact_substring_hits():
    hit = match_faq("你好，大概多久发货呀", FAQS)
    assert hit is not None and hit.score == 1.0
    assert hit.faq.answer.startswith("支付成功后")


def test_faq_containment_hits_without_substring():
    # "多久才发货" 里没有连续的 "多久发货"，靠 token 覆盖命中
    hit = match_faq("那到底多久才发货", [Faq("多久发货", "5 秒内")])
    assert hit is not None and 0.6 <= hit.score < 1.0


def test_faq_misses_unrelated_question():
    assert match_faq("今天天气不错", FAQS) is None


def test_single_token_question_only_matches_exactly():
    faq = Faq("发货", "很快")
    # 原文精确出现 → 命中。子串匹配是可靠信号，短问题也放行。
    assert match_faq("发货", [faq]) is not None
    # 只是零散出现、没有连续子串 → 单 token 问题不做模糊匹配
    assert match_faq("会发吗", [faq]) is None


def test_faq_empty_inputs():
    assert match_faq("", FAQS) is None
    assert match_faq("多久发货", []) is None


# ===========================================================================
# 二、上下文压缩（优化二）
# ===========================================================================

def test_digest_summarizes_facts():
    turns = [
        Turn("BUYER", "在吗"),
        Turn("BUYER", "便宜点"),
        Turn("AI", "给您 120", intent="BARGAIN", offered_price=120.0),
        Turn("AI", "给您 113", intent="BARGAIN", offered_price=113.0),
    ]
    text = digest_turns(turns)
    assert "4 轮" in text
    assert "买家发言 2 次" in text
    assert "最低 ¥113" in text
    assert "BARGAIN" in text


def test_digest_of_empty_is_empty():
    assert digest_turns([]) == ""


def test_short_conversation_is_not_compressed():
    turns = [Turn("BUYER", f"msg{i}") for i in range(3)]
    kept, summary = compress_context(turns, keep_recent=6)
    assert len(kept) == 3
    assert summary == ""


def test_long_conversation_keeps_recent_and_summarizes_rest():
    turns = [Turn("BUYER", f"msg{i}") for i in range(10)]
    kept, summary = compress_context(turns, keep_recent=6)
    assert len(kept) == 6
    assert kept[-1].content == "msg9"
    assert "4 轮" in summary


def test_char_cap_trims_even_more():
    turns = [Turn("BUYER", "x" * 400) for _ in range(6)]
    kept, _ = compress_context(turns, keep_recent=6, max_chars=1000)
    assert sum(len(t.content) for t in kept) <= 1000
    assert len(kept) < 6


# ===========================================================================
# 三、成本核算（优化三）
# ===========================================================================

def test_estimate_cost():
    assert estimate_cost(1_000_000, 0) == pytest.approx(1.0)
    assert estimate_cost(0, 1_000_000) == pytest.approx(2.0)
    assert estimate_cost(1000, 200) == pytest.approx(0.0014)


def test_usage_tracker_accumulates():
    tracker = UsageTracker()
    for _ in range(3):
        tracker.record_turn("s1", when=NOW)
    tracker.record_faq_hit("s1", when=NOW)
    tracker.record_faq_hit("s1", when=NOW)
    tracker.record_llm("s1", prompt_tokens=1000, completion_tokens=200, when=NOW)

    report = tracker.report("s1", when=NOW)
    assert report["turns"] == 3
    assert report["llm_calls"] == 1
    assert report["faq_hits"] == 2
    assert report["faq_hit_rate"] == pytest.approx(2 / 3, rel=1e-3)
    assert report["cost"] == pytest.approx(0.0014)
    assert report["saved_calls"] == 2


def test_usage_is_isolated_per_store():
    tracker = UsageTracker()
    tracker.record_turn("s1", when=NOW)
    tracker.record_turn("s2", when=NOW)
    tracker.record_turn("s2", when=NOW)
    assert tracker.report("s1", when=NOW)["turns"] == 1
    assert tracker.report("s2", when=NOW)["turns"] == 2


# ===========================================================================
# 四、系统提示词
# ===========================================================================

def test_system_prompt_carries_the_round_band():
    prompt = build_system_prompt(PRODUCT, round_no=1)
    assert "121.6" in prompt          # 128 * 0.95
    assert "128" in prompt
    assert "第 1 轮" in prompt


def test_system_prompt_band_tightens_by_round():
    # 第 2 轮让步上限 12% → 下限 128 * 0.88
    assert "112.64" in build_system_prompt(PRODUCT, round_no=2)


def test_system_prompt_without_product_still_bans_off_platform():
    prompt = build_system_prompt(None, round_no=1)
    assert "微信" in prompt and "禁止" in prompt


# ===========================================================================
# 五、主流程
# ===========================================================================

def test_duplicate_message_is_dropped_before_any_cost():
    async def scenario():
        store = MemoryIdempotencyStore()
        llm = llm_returning()
        usage = UsageTracker()
        first = await handle_turn(msg("多久发货", msg_id="dup"), store_id="s1",
                                  faqs=FAQS, llm=llm, dedup_store=store, usage=usage, now=NOW)
        second = await handle_turn(msg("多久发货", msg_id="dup"), store_id="s1",
                                   faqs=FAQS, llm=llm, dedup_store=store, usage=usage, now=NOW)
        return first, second, llm.calls, usage.report("s1", when=NOW)

    first, second, calls, report = run(scenario())
    assert first.action == Action.FAQ
    assert second.action == Action.DROP
    assert calls == 0                 # 全程没碰大模型
    assert report["turns"] == 1       # 重复消息不计入轮次


def test_ai_stays_silent_under_human_control():
    async def scenario():
        llm = llm_returning()
        result = await handle_turn(msg("在吗"), store_id="s1", conv_state=ConvState.HUMAN,
                                   llm=llm, now=NOW)
        return result, llm.calls

    result, calls = run(scenario())
    assert result.action == Action.SILENT
    assert calls == 0


def test_off_platform_message_gets_deflect_reply_and_no_llm_call():
    async def scenario():
        llm = llm_returning()
        result = await handle_turn(msg("加我微信 abc123"), store_id="s1", llm=llm, now=NOW)
        return result, llm.calls

    result, calls = run(scenario())
    assert result.action == Action.DEFLECT
    assert result.reply == DEFLECT_BLOCK
    assert calls == 0
    assert result.guard is not None and result.guard.blocked


def test_faq_hit_skips_the_model_and_is_counted():
    async def scenario():
        llm = llm_returning()
        usage = UsageTracker()
        result = await handle_turn(msg("请问多久发货"), store_id="s1", faqs=FAQS,
                                   llm=llm, usage=usage, now=NOW)
        return result, llm.calls, usage.report("s1", when=NOW)

    result, calls, report = run(scenario())
    assert result.action == Action.FAQ
    assert calls == 0
    assert report["faq_hits"] == 1
    assert report["faq_hit_rate"] == 1.0


def test_model_path_records_cost():
    async def scenario():
        llm = llm_returning("好的亲～")
        usage = UsageTracker()
        result = await handle_turn(msg("这个支持什么端"), store_id="s1", llm=llm,
                                   usage=usage, now=NOW)
        return result, llm.calls, usage.report("s1", when=NOW)

    result, calls, report = run(scenario())
    assert result.action == Action.AI
    assert calls == 1
    assert report["llm_calls"] == 1
    assert report["cost"] == pytest.approx(0.0014)


def test_valid_bargain_price_is_kept():
    async def scenario():
        # 第 1 轮区间 [121.6, 128]，124 落在区间内
        llm = llm_returning("给您 124 吧", offered_price=124.0, intent="BARGAIN")
        return await handle_turn(msg("便宜点"), store_id="s1", product=PRODUCT, llm=llm,
                                 round_no=1, now=NOW)

    result = run(scenario())
    assert result.offered_price == 124.0
    assert result.intent == "BARGAIN"
    assert result.reply == "给您 124 吧"


def test_out_of_band_price_is_replaced_by_fallback():
    async def scenario():
        # 第 1 轮下限 121.6，模型报 90 —— 必须整条作废
        llm = llm_returning("给您 90", offered_price=90.0, intent="BARGAIN")
        return await handle_turn(msg("便宜点"), store_id="s1", product=PRODUCT, llm=llm,
                                 round_no=1, now=NOW)

    result = run(scenario())
    assert result.offered_price is None
    assert result.reply == FALLBACK_REPLY
    assert "越界" in result.note


def test_explicit_human_request_hands_off_and_keeps_draft():
    async def scenario():
        llm = llm_returning("好的亲～")
        result = await handle_turn(msg("我要转人工"), store_id="s1", llm=llm, now=NOW)
        return result

    result = run(scenario())
    assert result.action == Action.HANDOFF
    assert result.reply == HANDOFF_HOLD_REPLY
    assert result.draft_reply == "好的亲～"      # 草稿留给人工，没有被覆盖
    assert result.ticket is not None
    assert Reason.EXPLICIT_REQUEST in result.ticket.reasons


def test_blocked_message_still_opens_a_ticket_on_complaint():
    async def scenario():
        llm = llm_returning()
        return await handle_turn(msg("我要投诉！加我微信"), store_id="s1", llm=llm, now=NOW)

    result = run(scenario())
    # 拦截优先：话术照发（止损比转接紧急），但工单同时开出来
    assert result.action == Action.DEFLECT
    assert result.reply == DEFLECT_BLOCK
    assert result.ticket is not None
    assert "同时已转人工" in result.note


def test_low_confidence_plus_unresolved_hands_off():
    async def scenario():
        llm = llm_returning(confidence=0.3)
        return await handle_turn(msg("那到底怎么弄"), store_id="s1", llm=llm,
                                 unresolved_turns=3, now=NOW)

    result = run(scenario())
    assert result.action == Action.HANDOFF
    assert {Reason.LOW_CONFIDENCE, Reason.REPEAT_UNRESOLVED} <= set(result.ticket.reasons)


def test_context_is_compressed_before_hitting_the_model():
    async def scenario():
        llm = llm_returning()
        history = [Turn("BUYER", f"第{i}轮") for i in range(12)]
        await handle_turn(msg("现在呢"), store_id="s1", llm=llm, context=history, now=NOW)
        return llm.last_kwargs["context"]

    kept = run(scenario())
    assert len(kept) == 6          # 只把最近 6 轮原文送进模型


def test_normal_turn_without_model_client_is_flagged():
    async def scenario():
        return await handle_turn(msg("在吗"), store_id="s1", now=NOW)

    result = run(scenario())
    assert result.action == Action.AI
    assert result.reply is None
    assert "未配置" in result.note
