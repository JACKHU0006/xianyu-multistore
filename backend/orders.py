"""
订单状态机与对账

为什么必须有这一层
------------------
原方案里完全没有订单这一层，直接"监听支付回调 → 发货"。这在真实业务里会漏掉
两类事故，而且都是要赔钱的：

  A. 发了货没付款
     回调被伪造、重复投递、或者状态乱序到达，导致货已经发给了一个没付钱的人。

  B. 付款了没发货
     回调丢了，订单躺在"待发货"里没人管，买家等到自动退款 + 差评 + 店铺扣分。

这两种事故的共同点是：**单看本地数据永远发现不了**，必须拿平台数据对一遍。
所以这个模块做两件事：

  1. 状态机 —— 让订单只能沿着合法路径走，非法流转在入口就被拒绝
  2. 对账   —— 定期把本地订单和平台快照比一遍，把"脱节"变成可推送的工单

对账的核心是一个**纯函数** `reconcile()`：输入两份字典，输出问题清单。
不碰数据库、不碰网络，所以可以穷举测试——这是它最大的价值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .guardrails import TenantCtx, scoped
from .models import Order, OrderStateLog

# 金额比较容差：浮点比较不能直接 ==
AMOUNT_EPSILON = 0.01
# 已支付但多久没发货就该报警
DEFAULT_UNSHIPPED_ALERT = timedelta(minutes=30)


# ===========================================================================
# 一、状态机
# ===========================================================================

class OrderStatus:
    WAIT_BUYER_PAY = "WAIT_BUYER_PAY"
    PAID = "PAID"
    SHIPPED = "SHIPPED"
    SUCCESS = "SUCCESS"
    REFUNDING = "REFUNDING"
    REFUNDED = "REFUNDED"
    CLOSED = "CLOSED"


ALL_STATUSES = frozenset({
    OrderStatus.WAIT_BUYER_PAY, OrderStatus.PAID, OrderStatus.SHIPPED,
    OrderStatus.SUCCESS, OrderStatus.REFUNDING, OrderStatus.REFUNDED,
    OrderStatus.CLOSED,
})

# 允许的流转。没列出来的组合一律非法。
TRANSITIONS: dict[str, frozenset[str]] = {
    OrderStatus.WAIT_BUYER_PAY: frozenset({OrderStatus.PAID, OrderStatus.CLOSED}),
    OrderStatus.PAID: frozenset({OrderStatus.SHIPPED, OrderStatus.REFUNDING, OrderStatus.CLOSED}),
    OrderStatus.SHIPPED: frozenset({OrderStatus.SUCCESS, OrderStatus.REFUNDING}),
    # 退款被驳回 → 回到已支付，这是真实存在的路径
    OrderStatus.REFUNDING: frozenset({OrderStatus.REFUNDED, OrderStatus.PAID}),
    OrderStatus.SUCCESS: frozenset({OrderStatus.REFUNDING}),
    OrderStatus.REFUNDED: frozenset(),
    OrderStatus.CLOSED: frozenset(),
}

TERMINAL_STATUSES = frozenset({OrderStatus.REFUNDED, OrderStatus.CLOSED})

# 只有"已支付"能发货。这是铁律，不是建议。
SHIPPABLE_STATUSES = frozenset({OrderStatus.PAID})

# 主轴上"越往后越深"的阶段，用来判断本地和平台谁滞后
_STAGE: dict[str, int] = {
    OrderStatus.WAIT_BUYER_PAY: 0,
    OrderStatus.PAID: 1,
    OrderStatus.SHIPPED: 2,
    OrderStatus.SUCCESS: 3,
}


class InvalidTransition(Exception):
    """非法状态流转。调用方应该把它当成 bug，而不是可恢复的业务异常。"""

    def __init__(self, frm: str, to: str) -> None:
        self.frm, self.to = frm, to
        super().__init__(f"订单状态不允许从 {frm} 流转到 {to}")


def can_transition(frm: str, to: str) -> bool:
    if frm not in ALL_STATUSES or to not in ALL_STATUSES:
        return False
    return to in TRANSITIONS.get(frm, frozenset())


def assert_transition(frm: str, to: str) -> None:
    if not can_transition(frm, to):
        raise InvalidTransition(frm, to)


def can_ship(status: str) -> bool:
    """能不能对这个状态的订单执行发货。"""
    return status in SHIPPABLE_STATUSES


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def stage_of(status: str) -> Optional[int]:
    """主轴阶段；退款/关闭这类离轴状态返回 None。"""
    return _STAGE.get(status)


def transition(
    order: Order,
    to: str,
    *,
    reason: Optional[str] = None,
    actor: str = "SYSTEM",
    now: Optional[datetime] = None,
) -> OrderStateLog:
    """
    执行一次状态流转，并留下痕迹。

    非法流转直接抛异常——不要静默忽略，静默忽略正是"发了货没付款"的成因。
    """
    frm = order.status
    assert_transition(frm, to)

    moment = now or datetime.now(timezone.utc)
    order.status = to
    if to == OrderStatus.PAID and order.paid_at is None:
        order.paid_at = moment
    if to == OrderStatus.SHIPPED and order.shipped_at is None:
        order.shipped_at = moment
    if to in TERMINAL_STATUSES:
        order.closed_at = moment

    log = OrderStateLog(
        tenant_id=order.tenant_id,
        order_id=order.id,
        from_status=frm,
        to_status=to,
        reason=reason,
        actor=actor,
        created_at=moment,
    )
    order.state_logs.append(log)
    return log


# ===========================================================================
# 二、对账（纯函数，可穷举测试）
# ===========================================================================

class IssueKind:
    MISSING_LOCAL = "MISSING_LOCAL"          # 平台有单，本地没有 → 可能漏发货
    MISSING_REMOTE = "MISSING_REMOTE"        # 本地有单，平台没有 → 脏数据
    OVER_SHIPPED = "OVER_SHIPPED"            # 已发货，但平台显示未付款/已关闭/已退款
    STATUS_MISMATCH = "STATUS_MISMATCH"      # 两边状态脱节
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"      # 金额对不上
    UNSHIPPED_TIMEOUT = "UNSHIPPED_TIMEOUT"  # 已支付但迟迟未发货


@dataclass(frozen=True)
class OrderView:
    """对账用的订单视图。本地和平台都用它，这样比较逻辑不需要两套。"""

    order_id: str
    status: str
    amount: float
    paid_at: Optional[datetime] = None
    shipped_at: Optional[datetime] = None


@dataclass(frozen=True)
class ReconcileIssue:
    kind: str
    severity: str          # P0 立即处理 / P1 当天处理 / P2 记录
    order_id: str
    detail: str


def status_mismatch_severity(local_status: str, remote_status: str) -> str:
    """
    同样是"状态不一致"，方向不同严重程度完全不同：

      平台比本地更靠后 → 本地滞后。最危险：平台已发货而本地还认为待发货，
                        下一轮扫描可能再发一次货。
      本地比平台更靠后 → 本地超前。可能是回调乱序，人工核对即可。
    """
    ls, rs = stage_of(local_status), stage_of(remote_status)
    if ls is not None and rs is not None:
        return "P0" if rs > ls else "P1"
    return "P1"


def _local_shipped(view: OrderView) -> bool:
    return view.shipped_at is not None or view.status in {
        OrderStatus.SHIPPED, OrderStatus.SUCCESS
    }


def _remote_unpaid(view: OrderView) -> bool:
    return view.status in {
        OrderStatus.WAIT_BUYER_PAY, OrderStatus.CLOSED, OrderStatus.REFUNDED
    }


def reconcile(
    local: dict[str, OrderView],
    remote: dict[str, OrderView],
) -> list[ReconcileIssue]:
    """
    把本地订单和平台快照比一遍，返回所有不一致。

    以平台为准判断"该不该发货"，以本地为准判断"已经发了什么"——
    因为钱在平台那边，货在我们这边。
    """
    issues: list[ReconcileIssue] = []

    for oid, r in remote.items():
        l = local.get(oid)

        if l is None:
            issues.append(ReconcileIssue(
                IssueKind.MISSING_LOCAL, "P0", oid,
                f"平台订单状态为 {r.status}（金额 ¥{r.amount}），本地无记录，可能漏单漏发",
            ))
            continue

        if abs(l.amount - r.amount) > AMOUNT_EPSILON:
            issues.append(ReconcileIssue(
                IssueKind.AMOUNT_MISMATCH, "P0", oid,
                f"金额不一致：本地 ¥{l.amount}，平台 ¥{r.amount}",
            ))

        if _local_shipped(l) and _remote_unpaid(r):
            issues.append(ReconcileIssue(
                IssueKind.OVER_SHIPPED, "P0", oid,
                f"本地已发货（{l.status}），但平台显示 {r.status} —— 存在超发风险",
            ))
        elif l.status != r.status:
            issues.append(ReconcileIssue(
                IssueKind.STATUS_MISMATCH,
                status_mismatch_severity(l.status, r.status),
                oid,
                f"状态脱节：本地 {l.status}，平台 {r.status}",
            ))

    for oid, l in local.items():
        if oid not in remote:
            issues.append(ReconcileIssue(
                IssueKind.MISSING_REMOTE, "P1", oid,
                f"本地订单（{l.status}，¥{l.amount}）在平台侧查不到，可能是脏数据或测试单",
            ))

    issues.sort(key=lambda i: (i.severity, i.kind, i.order_id))
    return issues


def find_stale_unshipped(
    orders: Iterable[OrderView],
    *,
    now: datetime,
    threshold: timedelta = DEFAULT_UNSHIPPED_ALERT,
) -> list[ReconcileIssue]:
    """
    找出"已支付但迟迟没发货"的订单 —— 事故 B。

    另外把"状态是已支付但没有支付时间"的也挑出来：这本身就是数据异常，
    而且会让超时判断失效，等于给漏发货开了后门。
    """
    issues: list[ReconcileIssue] = []
    for o in orders:
        if o.status != OrderStatus.PAID:
            continue
        if o.paid_at is None:
            issues.append(ReconcileIssue(
                IssueKind.UNSHIPPED_TIMEOUT, "P0", o.order_id,
                "订单已支付但缺少支付时间，无法判断超时，需人工核查",
            ))
            continue
        waited = now - o.paid_at
        if waited > threshold:
            minutes = int(waited.total_seconds() // 60)
            issues.append(ReconcileIssue(
                IssueKind.UNSHIPPED_TIMEOUT, "P0", o.order_id,
                f"已支付 {minutes} 分钟仍未发货（阈值 {int(threshold.total_seconds() // 60)} 分钟）",
            ))
    return issues


def summarize(issues: Sequence[ReconcileIssue]) -> dict:
    by_severity: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for i in issues:
        by_severity[i.severity] = by_severity.get(i.severity, 0) + 1
        by_kind[i.kind] = by_kind.get(i.kind, 0) + 1
    return {
        "total": len(issues),
        "by_severity": by_severity,
        "by_kind": by_kind,
        "has_p0": by_severity.get("P0", 0) > 0,
    }


def render_report(issues: Sequence[ReconcileIssue], store_name: str = "") -> str:
    stats = summarize(issues)
    head = f"对账结果{' · ' + store_name if store_name else ''}：共 {stats['total']} 项"
    if not issues:
        return head + "，本地与平台一致"
    lines = [head]
    for i in issues:
        lines.append(f"[{i.severity}] {i.kind} {i.order_id} —— {i.detail}")
    return "\n".join(lines)


# ===========================================================================
# 三、数据库读写
# ===========================================================================

async def load_local_views(
    db: AsyncSession,
    ctx: TenantCtx,
    store_id: str,
    *,
    since: Optional[datetime] = None,
) -> dict[str, OrderView]:
    stmt = scoped(select(Order).where(Order.store_id == store_id), ctx, Order)
    if since is not None:
        stmt = stmt.where(Order.created_at >= since)
    rows = (await db.execute(stmt)).scalars().all()
    return {
        o.platform_order_id: OrderView(
            order_id=o.platform_order_id,
            status=o.status,
            amount=o.amount,
            paid_at=o.paid_at,
            shipped_at=o.shipped_at,
        )
        for o in rows
    }


@dataclass
class SyncResult:
    created: int = 0
    advanced: int = 0
    rejected: list[ReconcileIssue] = field(default_factory=list)


async def apply_snapshot(
    db: AsyncSession,
    ctx: TenantCtx,
    store_id: str,
    snapshot: Iterable[OrderView],
    *,
    product_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> SyncResult:
    """
    把平台快照写回本地。

    关键约束：平台说状态变了，也必须走状态机。如果平台给出的流转在本地状态机里
    非法（比如平台说 CLOSED、本地已 SHIPPED），**不强行改**，而是记成问题上报。
    宁可留一条待人工核对的工单，也不要让本地状态被外部数据随便改写。
    """
    moment = now or datetime.now(timezone.utc)
    result = SyncResult()

    stmt = scoped(select(Order).where(Order.store_id == store_id), ctx, Order)
    existing = {o.platform_order_id: o for o in (await db.execute(stmt)).scalars().all()}

    for view in snapshot:
        order = existing.get(view.order_id)

        if order is None:
            order = Order(
                tenant_id=ctx.tenant_id,
                store_id=store_id,
                product_id=product_id,
                platform_order_id=view.order_id,
                buyer_id="",
                amount=view.amount,
                status=view.status,
                paid_at=view.paid_at,
                shipped_at=view.shipped_at,
                last_synced_at=moment,
            )
            db.add(order)
            result.created += 1
            continue

        order.last_synced_at = moment
        if order.status == view.status:
            continue

        if not can_transition(order.status, view.status):
            result.rejected.append(ReconcileIssue(
                IssueKind.STATUS_MISMATCH, "P0", view.order_id,
                f"平台要求 {order.status} → {view.status}，但该流转非法，已拒绝并转人工",
            ))
            continue

        transition(order, view.status, reason="平台对账同步", actor="RECONCILER", now=moment)
        result.advanced += 1

    return result
