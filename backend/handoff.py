"""
转人工接管

为什么必须有
------------
AI 客服最大的风险不是"答得不好"，而是**答不好还一直答**。买家已经投诉了、
已经问了三遍同一个问题、或者已经开始骂人，AI 还在那里重复"亲，我们支持七天
无理由退换哦" —— 这时候每多回一句，差评概率就高一截。

所以需要一套"什么时候该闭嘴换人"的判定。核心是两条：

  1. **分级而不是布尔**：单个弱信号不足以转人工，但弱信号叠加必须能触发。
     只有"投诉/明确要人工"才是无条件立即转。
  2. **不只看内容，还看过程**：连续 N 轮没解决、议价僵持不下、
     买家反复尝试站外交易 —— 这些都不是单条消息能看出来的。

判定函数 `decide()` 是纯函数，输入信号列表输出决策，可以穷举测试。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

# ===========================================================================
# 一、会话状态
# ===========================================================================

class ConvState:
    AI = "AI"                # AI 自动接待中
    WAITING = "WAITING"      # 已申请转人工，排队中
    HUMAN = "HUMAN"          # 人工已接管
    CLOSED = "CLOSED"        # 会话结束


CONV_TRANSITIONS: dict[str, frozenset[str]] = {
    ConvState.AI: frozenset({ConvState.WAITING, ConvState.CLOSED}),
    ConvState.WAITING: frozenset({ConvState.HUMAN, ConvState.AI, ConvState.CLOSED}),
    ConvState.HUMAN: frozenset({ConvState.AI, ConvState.CLOSED}),
    ConvState.CLOSED: frozenset(),
}


def can_transition(frm: str, to: str) -> bool:
    return to in CONV_TRANSITIONS.get(frm, frozenset())


def request_human(state: str) -> str:
    if not can_transition(state, ConvState.WAITING):
        raise ValueError(f"当前状态 {state} 不能申请转人工")
    return ConvState.WAITING


def assign_human(state: str) -> str:
    if not can_transition(state, ConvState.HUMAN):
        raise ValueError(f"当前状态 {state} 不能被接管")
    return ConvState.HUMAN


def resume_ai(state: str) -> str:
    """人工处理完交还给 AI。只允许从 WAITING / HUMAN 交回。"""
    if not can_transition(state, ConvState.AI):
        raise ValueError(f"当前状态 {state} 不能交还给 AI")
    return ConvState.AI


def ai_should_stay_silent(state: str) -> bool:
    """AI 是否必须闭嘴（排队中或人工已接管）。"""
    return state in (ConvState.WAITING, ConvState.HUMAN)


# ===========================================================================
# 二、信号
# ===========================================================================

class Reason:
    EXPLICIT_REQUEST = "EXPLICIT_REQUEST"      # 明确要求人工
    COMPLAINT = "COMPLAINT"                    # 投诉 / 举报 / 差评威胁
    OFF_PLATFORM_HIT = "OFF_PLATFORM_HIT"      # 站外引流拦截命中
    LOW_CONFIDENCE = "LOW_CONFIDENCE"          # 模型自评置信度低
    REPEAT_UNRESOLVED = "REPEAT_UNRESOLVED"    # 连续多轮未解决
    NEGATIVE_SENTIMENT = "NEGATIVE_SENTIMENT"  # 负面情绪
    BARGAIN_DEADLOCK = "BARGAIN_DEADLOCK"      # 议价僵持


# 无条件立即转人工的理由 —— 这两种情况再让 AI 多说一句都是错的
IMMEDIATE_REASONS = frozenset({Reason.EXPLICIT_REQUEST, Reason.COMPLAINT})

# 各信号的权重。数值不是玄学，是按"误转成本 vs 漏转成本"调的：
# 漏转一个投诉 = 差评 + 扣分；误转一个普通咨询 = 人工看一眼，成本低得多。
SIGNAL_WEIGHTS: dict[str, int] = {
    Reason.OFF_PLATFORM_HIT: 50,
    Reason.LOW_CONFIDENCE: 40,
    Reason.REPEAT_UNRESOLVED: 35,
    Reason.NEGATIVE_SENTIMENT: 30,
    Reason.BARGAIN_DEADLOCK: 25,
}

DEFAULT_THRESHOLD = 60
P0_THRESHOLD = 80


@dataclass(frozen=True)
class Signal:
    reason: str
    weight: int
    detail: str


@dataclass(frozen=True)
class Decision:
    should_handoff: bool
    score: int
    priority: str                    # P0 / P1 / P2
    triggers: tuple[Signal, ...] = ()

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(s.reason for s in self.triggers)


# ===========================================================================
# 三、情绪与意图识别（纯函数）
# ===========================================================================

NEGATIVE_LEXICON: dict[str, int] = {
    "骗子": 40, "骗人": 40, "欺骗": 40, "假货": 40, "劣质": 35, "破损": 30,
    "投诉": 45, "举报": 45, "差评": 45, "曝光": 40, "报警": 50, "12315": 50,
    "退款": 25, "退货": 25, "垃圾": 30, "太差": 30, "很差": 30, "无语": 20,
    "生气": 25, "气死": 30, "恶心": 35, "上当": 35, "坑人": 30,
    "敷衍": 30, "不管": 25, "没人理": 30, "什么玩意": 35,
}

EXPLICIT_REQUEST_PATTERNS = (
    "转人工", "人工客服", "找人工", "要人工", "真人客服", "找客服",
    "叫你们主管", "找店长", "你们老板", "负责人出来",
)

COMPLAINT_PATTERNS = (
    "投诉", "举报", "差评", "曝光", "报警", "12315", "消协", "工商",
)


def score_sentiment(text: str) -> tuple[int, tuple[str, ...]]:
    """
    负面情绪打分。返回 (分数, 命中词)。

    取累加而不是取最大值：一句话里同时出现"骗子"和"投诉"，强度确实更高。
    但设上限 100，避免长消息靠堆词刷分。
    """
    if not text:
        return 0, ()
    hits = tuple(w for w in NEGATIVE_LEXICON if w in text)
    score = min(100, sum(NEGATIVE_LEXICON[w] for w in hits))
    return score, hits


def wants_human(text: str) -> bool:
    return any(p in text for p in EXPLICIT_REQUEST_PATTERNS)


def is_complaint(text: str) -> bool:
    return any(p in text for p in COMPLAINT_PATTERNS)


# ===========================================================================
# 四、决策
# ===========================================================================

def build_signals(
    *,
    text: str = "",
    off_platform_blocked: bool = False,
    confidence: Optional[float] = None,
    confidence_floor: float = 0.55,
    unresolved_turns: int = 0,
    unresolved_limit: int = 3,
    bargain_rounds: int = 0,
    bargain_limit: int = 4,
) -> list[Signal]:
    """把各种原始观测翻译成统一格式的信号。"""
    signals: list[Signal] = []

    if wants_human(text):
        signals.append(Signal(Reason.EXPLICIT_REQUEST, 100, "买家明确要求人工"))
    if is_complaint(text):
        signals.append(Signal(Reason.COMPLAINT, 100, "消息包含投诉/举报倾向"))

    if off_platform_blocked:
        signals.append(Signal(Reason.OFF_PLATFORM_HIT, SIGNAL_WEIGHTS[Reason.OFF_PLATFORM_HIT],
                              "买家尝试引导站外交易"))

    if confidence is not None and confidence < confidence_floor:
        signals.append(Signal(Reason.LOW_CONFIDENCE, SIGNAL_WEIGHTS[Reason.LOW_CONFIDENCE],
                              f"模型置信度 {confidence:.2f} 低于 {confidence_floor:.2f}"))

    if unresolved_turns >= unresolved_limit:
        signals.append(Signal(Reason.REPEAT_UNRESOLVED, SIGNAL_WEIGHTS[Reason.REPEAT_UNRESOLVED],
                              f"连续 {unresolved_turns} 轮未解决"))

    if bargain_rounds >= bargain_limit:
        signals.append(Signal(Reason.BARGAIN_DEADLOCK, SIGNAL_WEIGHTS[Reason.BARGAIN_DEADLOCK],
                              f"议价已进行 {bargain_rounds} 轮仍僵持"))

    sentiment, hits = score_sentiment(text)
    if sentiment >= 30:
        signals.append(Signal(Reason.NEGATIVE_SENTIMENT, SIGNAL_WEIGHTS[Reason.NEGATIVE_SENTIMENT],
                              f"负面情绪 {sentiment} 分（{'、'.join(hits)}）"))

    return signals


def decide(signals: Sequence[Signal], threshold: int = DEFAULT_THRESHOLD) -> Decision:
    """
    汇总信号并给出是否转人工。

    同一个理由只算一次分（取最大权重），避免"连续三轮未解决"被记三次分
    直接把阈值冲爆 —— 那是同一件事的重复计数，不是三个独立证据。
    """
    if not signals:
        return Decision(False, 0, "P2", ())

    best: dict[str, Signal] = {}
    for s in signals:
        if s.reason not in best or s.weight > best[s.reason].weight:
            best[s.reason] = s

    triggers = tuple(sorted(best.values(), key=lambda s: -s.weight))

    if any(t.reason in IMMEDIATE_REASONS for t in triggers):
        return Decision(True, 100, "P0", triggers)

    score = min(100, sum(t.weight for t in triggers))
    should = score >= threshold
    priority = "P0" if score >= P0_THRESHOLD else "P1" if should else "P2"
    return Decision(should, score, priority, triggers)


# ===========================================================================
# 五、工单与队列
# ===========================================================================

SLA: dict[str, timedelta] = {
    "P0": timedelta(minutes=5),
    "P1": timedelta(minutes=15),
}

# 超过 SLA 的多少比例算"即将超时"
AT_RISK_RATIO = 0.7


@dataclass
class Ticket:
    store_id: str
    buyer_id: str
    priority: str
    score: int
    reasons: tuple[str, ...]
    opened_at: datetime
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    assigned_to: Optional[str] = None
    closed_at: Optional[datetime] = None

    @property
    def open(self) -> bool:
        return self.closed_at is None


def open_ticket(
    decision: Decision,
    *,
    store_id: str,
    buyer_id: str,
    now: Optional[datetime] = None,
) -> Optional[Ticket]:
    if not decision.should_handoff:
        return None
    return Ticket(
        store_id=store_id,
        buyer_id=buyer_id,
        priority=decision.priority,
        score=decision.score,
        reasons=decision.reasons,
        opened_at=now or datetime.now(timezone.utc),
    )


def waiting_minutes(ticket: Ticket, now: datetime) -> float:
    end = ticket.closed_at or now
    return (end - ticket.opened_at).total_seconds() / 60


def sla_state(ticket: Ticket, now: datetime) -> str:
    """OK / AT_RISK / BREACHED。"""
    limit = SLA.get(ticket.priority, SLA["P1"])
    waited = (now - ticket.opened_at)
    if waited >= limit:
        return "BREACHED"
    if waited >= limit * AT_RISK_RATIO:
        return "AT_RISK"
    return "OK"


def pick_next(tickets: Iterable[Ticket], now: datetime) -> Optional[Ticket]:
    """
    下一个该处理的工单：先看优先级，同级里等最久的优先。

    不能只按优先级排 —— 三个 P1 排在一起时，先来后到才是公平的。
    """
    open_ones = [t for t in tickets if t.open]
    if not open_ones:
        return None
    order = {"P0": 0, "P1": 1, "P2": 2}
    return min(open_ones, key=lambda t: (order.get(t.priority, 9), t.opened_at))


def queue_summary(tickets: Iterable[Ticket], now: datetime) -> dict:
    open_ones = [t for t in tickets if t.open]
    by_priority: dict[str, int] = {}
    breached = 0
    for t in open_ones:
        by_priority[t.priority] = by_priority.get(t.priority, 0) + 1
        if sla_state(t, now) == "BREACHED":
            breached += 1
    oldest = max((waiting_minutes(t, now) for t in open_ones), default=0.0)
    return {
        "open": len(open_ones),
        "by_priority": by_priority,
        "breached": breached,
        "oldest_wait_minutes": round(oldest, 1),
        "needs_attention": breached > 0,
    }


def render_queue(summary: dict) -> str:
    if summary["open"] == 0:
        return "人工队列为空"
    parts = [f"待处理 {summary['open']} 单"]
    if summary["by_priority"]:
        detail = "、".join(f"{k} {v}" for k, v in sorted(summary["by_priority"].items()))
        parts.append(f"（{detail}）")
    parts.append(f"，最长等待 {summary['oldest_wait_minutes']} 分钟")
    if summary["breached"]:
        parts.append(f"，已超时 {summary['breached']} 单")
    return "".join(parts)
