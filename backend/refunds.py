"""
退款与售后

虚拟商品退款的特殊之处
----------------------
实物商品可以"退货退款"，货退回来损失可控。虚拟商品不行：卡密一旦发出去，
买家已经看到了，退不回来。所以这里的默认立场是**已交付的虚拟商品不适用
无理由退款** —— 这不是霸王条款，是虚拟商品的交易常识，平台规则也是支持的。

但"一律不退"同样是错的。有三种情况必须退，而且应该自动退，不要让人去点按钮：

  1. **卡密无效**。这是我们的问题，买家拿到的是一串废码。
     多拖一分钟，差评概率就高一分。
  2. **未发货**。货根本没出去，退款零损失，没有任何理由卡着。
  3. **重复购买**。同一订单重复支付，退掉多余的那笔。

剩下的走人工审核。但人工审核也不能瞎审，得看买家画像 —— 一个退款率 60%、
还试图把你引到站外交易的账号，和一个三年只退过一次的账号，不该用同一套标准。

注意：`abuse_score` 高**不会**直接导致自动拒绝。自动拒绝高风险买家是很容易
误伤真实用户的（老买家偶尔也会退一次）。它的作用是**升级到人工**，
让人带着上下文去看。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


class RefundReason:
    NOT_DELIVERED = "NOT_DELIVERED"          # 没收到货
    CARD_INVALID = "CARD_INVALID"            # 卡密无效 / 用不了
    CARD_ALREADY_USED = "CARD_ALREADY_USED"  # 卡密已被使用
    WRONG_ITEM = "WRONG_ITEM"                # 发错货
    NO_LONGER_NEEDED = "NO_LONGER_NEEDED"    # 不想要了
    DUPLICATE_PURCHASE = "DUPLICATE_PURCHASE"
    OTHER = "OTHER"


ALL_REASONS = frozenset({
    RefundReason.NOT_DELIVERED, RefundReason.CARD_INVALID,
    RefundReason.CARD_ALREADY_USED, RefundReason.WRONG_ITEM,
    RefundReason.NO_LONGER_NEEDED, RefundReason.DUPLICATE_PURCHASE,
    RefundReason.OTHER,
})


class Decision:
    AUTO_APPROVE = "AUTO_APPROVE"
    REVIEW = "REVIEW"
    AUTO_REJECT = "AUTO_REJECT"


# 买家责任的理由：货已经交付了，这些理由不该自动退
_BUYER_FAULT = frozenset({
    RefundReason.NO_LONGER_NEEDED,
    RefundReason.CARD_ALREADY_USED,
})

# 卖家责任的理由：自动退，别让人去点按钮
_SELLER_FAULT = frozenset({
    RefundReason.CARD_INVALID,
    RefundReason.WRONG_ITEM,
})

# 各决策的处理时限（分钟）
SLA_MINUTES: dict[str, int] = {
    Decision.AUTO_APPROVE: 5,
    Decision.REVIEW: 120,
    Decision.AUTO_REJECT: 30,
}

ABUSE_THRESHOLD = 60


@dataclass(frozen=True)
class BuyerHistory:
    total_orders: int = 0
    refunds: int = 0
    off_platform_strikes: int = 0
    complaints: int = 0


@dataclass(frozen=True)
class RefundDecision:
    decision: str
    reason: str
    requires_proof: bool
    flag_buyer: bool
    sla_minutes: int
    note: str

    @property
    def auto_handled(self) -> bool:
        return self.decision != Decision.REVIEW

    @property
    def deadline(self) -> timedelta:
        return timedelta(minutes=self.sla_minutes)


# ===========================================================================
# 一、买家画像
# ===========================================================================

def abuse_score(history: BuyerHistory) -> int:
    """
    0-100 的风险分。三项加权：

      退款率      最高 40 分 —— 最直接的信号
      站外引流    每次 10 分，最高 30 —— 站外交易纠纷率极高
      投诉        每次 15 分，最高 30

    退款率只在新客有足够样本时才计分（少于 3 单不算），
    否则"买 1 单退 1 单"会直接被算成 100 分，误伤第一次购物的人。
    """
    score = 0

    if history.total_orders >= 3:
        rate = history.refunds / history.total_orders
        score += min(40, int(rate * 40))

    score += min(30, history.off_platform_strikes * 10)
    score += min(30, history.complaints * 15)

    return min(100, score)


def is_abusive(score: int, threshold: int = ABUSE_THRESHOLD) -> bool:
    return score >= threshold


# ===========================================================================
# 二、决策
# ===========================================================================

def decide(
    reason: str,
    *,
    delivered: bool,
    card_revealed: bool = False,
    history: Optional[BuyerHistory] = None,
    duplicate_order: bool = False,
) -> RefundDecision:
    """
    给出退款处理意见。

    判定顺序是有讲究的 —— 从"确定该退"到"确定不该退"，中间落到人工：
    先看卖家责任和未发货（必退），再看买家责任（不该退），最后剩下的走审核。
    """
    # --- 必退：货没出去，退款零损失 ---
    # 放在最前面：未发货是**客观事实**，与买家说了什么理由无关。
    # 不该因为理由文本没被识别，就把一笔零损失的退款压给人工去点按钮。
    if not delivered:
        return RefundDecision(
            Decision.AUTO_APPROVE, reason, requires_proof=False, flag_buyer=False,
            sla_minutes=SLA_MINUTES[Decision.AUTO_APPROVE],
            note="订单尚未发货，直接退款，无需买家举证",
        )

    if reason not in ALL_REASONS:
        return RefundDecision(
            Decision.REVIEW, reason, requires_proof=True, flag_buyer=False,
            sla_minutes=SLA_MINUTES[Decision.REVIEW],
            note="无法识别的退款理由，转人工判断",
        )

    # --- 必退：重复支付 ---
    if duplicate_order or reason == RefundReason.DUPLICATE_PURCHASE:
        return RefundDecision(
            Decision.AUTO_APPROVE, reason, requires_proof=False, flag_buyer=False,
            sla_minutes=SLA_MINUTES[Decision.AUTO_APPROVE],
            note="重复支付，退掉多余的一笔",
        )

    # --- 必退：卖家责任 ---
    if reason in _SELLER_FAULT:
        return RefundDecision(
            Decision.AUTO_APPROVE, reason, requires_proof=True, flag_buyer=False,
            sla_minutes=SLA_MINUTES[Decision.AUTO_APPROVE],
            note="我方责任（卡密无效/发错货），立即退款并补发或补偿",
        )

    # --- 不该退：已交付的虚拟商品，买家自身原因 ---
    if reason in _BUYER_FAULT and card_revealed:
        score = abuse_score(history) if history else 0
        if is_abusive(score):
            # 高风险买家不能自动拒 —— 转人工带着画像去看，避免误伤
            return RefundDecision(
                Decision.REVIEW, reason, requires_proof=True, flag_buyer=True,
                sla_minutes=SLA_MINUTES[Decision.REVIEW],
                note=f"买家风险分 {score}，虽属买家责任仍转人工复核",
            )
        return RefundDecision(
            Decision.AUTO_REJECT, reason, requires_proof=False, flag_buyer=False,
            sla_minutes=SLA_MINUTES[Decision.AUTO_REJECT],
            note="卡密已交付且已被查看，虚拟商品不支持无理由退款",
        )

    # --- 其余走人工 ---
    score = abuse_score(history) if history else 0
    flagged = is_abusive(score)
    return RefundDecision(
        Decision.REVIEW, reason, requires_proof=True, flag_buyer=flagged,
        sla_minutes=SLA_MINUTES[Decision.REVIEW],
        note=("买家风险分偏高，请重点核对" if flagged else "需人工核对发货与沟通记录"),
    )


def render(decision: RefundDecision) -> str:
    tag = {
        Decision.AUTO_APPROVE: "【自动同意】",
        Decision.REVIEW: "【转人工】",
        Decision.AUTO_REJECT: "【自动拒绝】",
    }.get(decision.decision, "【待定】")
    proof = "，需买家举证" if decision.requires_proof else ""
    flag = "，已标记买家" if decision.flag_buyer else ""
    return (
        f"{tag}{decision.reason}：{decision.note}"
        f"（{decision.sla_minutes} 分钟内处理{proof}{flag}）"
    )


# ===========================================================================
# 三、售后工单
# ===========================================================================

@dataclass
class RefundTicket:
    order_id: str
    store_id: str
    buyer_id: str
    decision: RefundDecision
    opened_at: datetime
    closed_at: Optional[datetime] = None
    closed_by: Optional[str] = None

    @property
    def open(self) -> bool:
        return self.closed_at is None

    def overdue(self, now: datetime) -> bool:
        return self.open and (now - self.opened_at) > self.decision.deadline


def open_ticket(
    *,
    order_id: str,
    store_id: str,
    buyer_id: str,
    decision: RefundDecision,
    now: Optional[datetime] = None,
) -> Optional[RefundTicket]:
    """只有需要人工处理的才开工单；自动同意/拒绝的不占人工队列。"""
    if decision.auto_handled:
        return None
    return RefundTicket(
        order_id=order_id, store_id=store_id, buyer_id=buyer_id,
        decision=decision, opened_at=now or datetime.now(timezone.utc),
    )


def queue_summary(tickets: list[RefundTicket], now: datetime) -> dict:
    open_ones = [t for t in tickets if t.open]
    overdue = [t for t in open_ones if t.overdue(now)]
    flagged = [t for t in open_ones if t.decision.flag_buyer]
    oldest = max(
        ((now - t.opened_at).total_seconds() / 60 for t in open_ones), default=0.0
    )
    return {
        "open": len(open_ones),
        "overdue": len(overdue),
        "flagged": len(flagged),
        "oldest_wait_minutes": round(oldest, 1),
        "needs_attention": bool(overdue),
    }
