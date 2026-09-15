"""
转人工接管测试

核心断言：单个弱信号不该转人工（否则人工会被淹没），
但弱信号叠加必须能触发（否则差评会漏出去）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.handoff import (
    ConvState,
    Decision,
    Reason,
    Signal,
    SLA,
    Ticket,
    ai_should_stay_silent,
    assign_human,
    build_signals,
    can_transition,
    decide,
    is_complaint,
    open_ticket,
    pick_next,
    queue_summary,
    render_queue,
    request_human,
    resume_ai,
    score_sentiment,
    sla_state,
    waiting_minutes,
    wants_human,
)

NOW = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)


# ===========================================================================
# 一、会话状态机
# ===========================================================================

def test_legal_conversation_transitions():
    assert can_transition(ConvState.AI, ConvState.WAITING)
    assert can_transition(ConvState.WAITING, ConvState.HUMAN)
    assert can_transition(ConvState.HUMAN, ConvState.AI)
    assert not can_transition(ConvState.CLOSED, ConvState.AI)


def test_handoff_cycle():
    state = request_human(ConvState.AI)
    assert state == ConvState.WAITING
    state = assign_human(state)
    assert state == ConvState.HUMAN
    assert resume_ai(state) == ConvState.AI


def test_cannot_request_human_on_closed_conversation():
    with pytest.raises(ValueError):
        request_human(ConvState.CLOSED)


def test_cannot_assign_human_directly_from_ai():
    # 必须先排队，不能从 AI 直接跳到人工，否则队列统计会失真
    with pytest.raises(ValueError):
        assign_human(ConvState.AI)


def test_ai_stays_silent_while_waiting_or_taken_over():
    assert ai_should_stay_silent(ConvState.WAITING)
    assert ai_should_stay_silent(ConvState.HUMAN)
    assert not ai_should_stay_silent(ConvState.AI)


# ===========================================================================
# 二、情绪与意图
# ===========================================================================

def test_sentiment_scoring():
    score, hits = score_sentiment("你们这是骗子吧")
    assert score >= 40 and "骗子" in hits


def test_sentiment_accumulates_but_is_capped():
    score, _ = score_sentiment("骗子 假货 投诉 举报 报警 恶心 垃圾")
    assert score == 100


def test_neutral_text_scores_zero():
    assert score_sentiment("这个多久发货呀")[0] == 0


def test_explicit_human_request_detection():
    assert wants_human("我要转人工")
    assert wants_human("叫你们主管出来")
    assert not wants_human("这个多少钱")


def test_complaint_detection():
    assert is_complaint("再不发货我就投诉了")
    assert is_complaint("我要打12315")
    assert not is_complaint("发货很快，谢谢")


# ===========================================================================
# 三、决策
# ===========================================================================

def test_single_weak_signal_does_not_handoff():
    d = decide([Signal(Reason.LOW_CONFIDENCE, 40, "x")])
    assert not d.should_handoff
    assert d.priority == "P2"


def test_weak_signals_combine_into_handoff():
    d = decide([
        Signal(Reason.LOW_CONFIDENCE, 40, "x"),
        Signal(Reason.REPEAT_UNRESOLVED, 35, "y"),
    ])
    assert d.should_handoff
    assert d.score == 75
    assert d.priority == "P1"


def test_off_platform_plus_sentiment_is_p0():
    d = decide([
        Signal(Reason.OFF_PLATFORM_HIT, 50, "x"),
        Signal(Reason.NEGATIVE_SENTIMENT, 30, "y"),
    ])
    assert d.should_handoff and d.priority == "P0"


def test_explicit_request_is_immediate_regardless_of_score():
    d = decide([Signal(Reason.EXPLICIT_REQUEST, 100, "买家要求人工")])
    assert d.should_handoff and d.priority == "P0"


def test_complaint_is_immediate():
    d = decide([Signal(Reason.COMPLAINT, 100, "投诉")])
    assert d.should_handoff and d.priority == "P0"


def test_same_reason_is_not_double_counted():
    # 同一件事报三次不该把阈值冲爆
    d = decide([
        Signal(Reason.LOW_CONFIDENCE, 40, "a"),
        Signal(Reason.LOW_CONFIDENCE, 40, "b"),
        Signal(Reason.LOW_CONFIDENCE, 40, "c"),
    ])
    assert d.score == 40
    assert not d.should_handoff


def test_same_reason_keeps_the_heaviest_signal():
    d = decide([
        Signal(Reason.NEGATIVE_SENTIMENT, 10, "轻"),
        Signal(Reason.NEGATIVE_SENTIMENT, 30, "重"),
    ])
    assert d.score == 30


def test_no_signals_means_no_handoff():
    d = decide([])
    assert not d.should_handoff and d.score == 0


# ===========================================================================
# 四、信号构造
# ===========================================================================

def test_build_signals_from_raw_observations():
    signals = build_signals(
        text="骗子！我要投诉",
        off_platform_blocked=True,
        confidence=0.3,
        unresolved_turns=4,
        bargain_rounds=5,
    )
    reasons = {s.reason for s in signals}
    assert Reason.COMPLAINT in reasons
    assert Reason.OFF_PLATFORM_HIT in reasons
    assert Reason.LOW_CONFIDENCE in reasons
    assert Reason.REPEAT_UNRESOLVED in reasons
    assert Reason.BARGAIN_DEADLOCK in reasons
    assert Reason.NEGATIVE_SENTIMENT in reasons


def test_build_signals_is_quiet_on_a_normal_question():
    assert build_signals(text="多久发货呀", confidence=0.9) == []


def test_confidence_floor_boundary():
    assert build_signals(text="x", confidence=0.55) == []
    assert Reason.LOW_CONFIDENCE in {s.reason for s in build_signals(text="x", confidence=0.54)}


# ===========================================================================
# 五、工单与队列
# ===========================================================================

def _p0_ticket(opened_at=NOW, buyer="b1"):
    return Ticket(store_id="s1", buyer_id=buyer, priority="P0", score=100,
                  reasons=(Reason.COMPLAINT,), opened_at=opened_at)


def test_open_ticket_only_when_handoff():
    assert open_ticket(decide([]), store_id="s1", buyer_id="b1", now=NOW) is None
    ticket = open_ticket(
        decide([Signal(Reason.COMPLAINT, 100, "投诉")]), store_id="s1", buyer_id="b1", now=NOW
    )
    assert ticket is not None and ticket.priority == "P0"


def test_waiting_minutes():
    t = _p0_ticket()
    assert waiting_minutes(t, NOW + timedelta(minutes=3)) == 3.0


def test_sla_states():
    t = _p0_ticket()
    assert sla_state(t, NOW + timedelta(minutes=1)) == "OK"
    assert sla_state(t, NOW + timedelta(minutes=4)) == "AT_RISK"
    assert sla_state(t, NOW + timedelta(minutes=6)) == "BREACHED"


def test_p1_gets_a_longer_sla():
    t = Ticket(store_id="s1", buyer_id="b1", priority="P1", score=75,
               reasons=(Reason.LOW_CONFIDENCE,), opened_at=NOW)
    assert sla_state(t, NOW + timedelta(minutes=6)) == "OK"
    assert SLA["P1"] > SLA["P0"]


def test_pick_next_prefers_priority_then_fifo():
    old_p1 = Ticket(store_id="s1", buyer_id="a", priority="P1", score=75,
                    reasons=(), opened_at=NOW - timedelta(minutes=10))
    new_p0 = Ticket(store_id="s1", buyer_id="b", priority="P0", score=100,
                    reasons=(), opened_at=NOW - timedelta(minutes=1))
    assert pick_next([old_p1, new_p0], NOW) is new_p0

    older_p1 = Ticket(store_id="s1", buyer_id="c", priority="P1", score=75,
                      reasons=(), opened_at=NOW - timedelta(minutes=20))
    assert pick_next([old_p1, older_p1], NOW) is older_p1


def test_pick_next_ignores_closed_tickets():
    closed = _p0_ticket()
    closed.closed_at = NOW
    assert pick_next([closed], NOW) is None


def test_queue_summary_counts_and_flags_breach():
    tickets = [_p0_ticket(), Ticket(store_id="s1", buyer_id="b2", priority="P1",
                                    score=75, reasons=(), opened_at=NOW)]
    summary = queue_summary(tickets, NOW + timedelta(minutes=6))
    assert summary["open"] == 2
    assert summary["by_priority"] == {"P0": 1, "P1": 1}
    assert summary["breached"] == 1
    assert summary["needs_attention"] is True


def test_queue_summary_when_empty():
    summary = queue_summary([], NOW)
    assert summary["open"] == 0 and summary["needs_attention"] is False
    assert render_queue(summary) == "人工队列为空"


def test_render_queue_mentions_backlog():
    tickets = [_p0_ticket()]
    text = render_queue(queue_summary(tickets, NOW + timedelta(minutes=6)))
    assert "待处理 1 单" in text and "已超时" in text
