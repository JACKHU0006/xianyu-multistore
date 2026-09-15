"""
告警分级与路由

为什么不能"所有告警都发飞书"
----------------------------
因为告警一旦全部同质化，人就会开始忽略它。这是告警系统最常见的死法：
先是 P2 的补货提醒刷屏，然后是 P1 的工单超时被划过去，最后 P0 的"付款了
没发货"也没人看 —— 而那个是要赔钱的。

所以按严重程度分配**不同成本**的通知通道：

    P0  电话 + IM      必须吵醒人。且**永不静默** —— 同一条 P0 重复响是应该的
    P1  IM             当天处理，有 15 分钟静默窗口防止抖动刷屏
    P2  日报汇总        不用即时打扰，攒到日报里一起看

另外两条：
  - **未确认要升级**。P1 发出去 30 分钟没人接，升级到电话。
    告警系统的价值不在于"发出去了"，而在于"有人处理了"。
  - **值班表要有空洞检测**。P0 告警发出去没人接，多半是排班漏了，
    这个本身就该是一条告警。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence


class Severity:
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class Channel:
    PHONE = "PHONE"
    IM = "IM"
    EMAIL = "EMAIL"
    DIGEST = "DIGEST"


class Kind:
    STOCK_OUT = "STOCK_OUT"
    STOCK_LOW = "STOCK_LOW"
    UNSHIPPED_TIMEOUT = "UNSHIPPED_TIMEOUT"
    OVER_SHIPPED = "OVER_SHIPPED"
    MISSING_LOCAL = "MISSING_LOCAL"
    OFF_PLATFORM = "OFF_PLATFORM"
    QUEUE_BREACH = "QUEUE_BREACH"
    RISK_EVENT = "RISK_EVENT"
    CRITICAL_CHANGE = "CRITICAL_CHANGE"


ROUTING: dict[str, tuple[str, ...]] = {
    Severity.P0: (Channel.PHONE, Channel.IM),
    Severity.P1: (Channel.IM,),
    Severity.P2: (Channel.DIGEST,),
}

# 升级时补发的通道（原始通道已经发过，这里只补"更贵"的那个）
ESCALATION_CHANNELS: dict[str, tuple[str, ...]] = {
    Severity.P0: (Channel.PHONE,),
    Severity.P1: (Channel.PHONE,),
    Severity.P2: (Channel.IM,),
}

REQUIRES_ACK = frozenset({Severity.P0, Severity.P1})

# P0 是零窗口：同一条 P0 重复响是特性不是 bug
SILENCE_WINDOW: dict[str, timedelta] = {
    Severity.P0: timedelta(0),
    Severity.P1: timedelta(minutes=15),
    Severity.P2: timedelta(hours=24),
}

ESCALATE_AFTER: dict[str, Optional[timedelta]] = {
    Severity.P0: timedelta(minutes=5),
    Severity.P1: timedelta(minutes=30),
    Severity.P2: None,
}


# ===========================================================================
# 一、告警对象
# ===========================================================================

@dataclass(frozen=True)
class Alert:
    severity: str
    kind: str
    store_id: str
    title: str
    detail: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    dedupe_key: str = ""
    ack_at: Optional[datetime] = None
    acked_by: Optional[str] = None

    @property
    def acknowledged(self) -> bool:
        return self.ack_at is not None

    def acknowledged_by(self, person: str, now: Optional[datetime] = None) -> "Alert":
        """返回确认后的新对象（frozen，不原地改）。"""
        from dataclasses import replace

        return replace(self, ack_at=now or datetime.now(timezone.utc), acked_by=person)


def make_alert(
    *,
    severity: str,
    kind: str,
    store_id: str,
    title: str,
    detail: str = "",
    created_at: Optional[datetime] = None,
    dedupe_key: Optional[str] = None,
) -> Alert:
    """
    同店同类事件默认合并成一条 dedupe_key。

    不加这个的话，卡密池空了以后每分钟扫一次就推一次，两小时能推 120 条，
    然后所有人都把告警静音了 —— 包括那条真正重要的。
    """
    return Alert(
        severity=severity,
        kind=kind,
        store_id=store_id,
        title=title,
        detail=detail,
        created_at=created_at or datetime.now(timezone.utc),
        dedupe_key=dedupe_key or f"{kind}:{store_id}",
    )


# ===========================================================================
# 二、路由决策（纯函数）
# ===========================================================================

def channels_for(alert: Alert) -> tuple[str, ...]:
    return ROUTING.get(alert.severity, (Channel.IM,))


def requires_ack(alert: Alert) -> bool:
    return alert.severity in REQUIRES_ACK


def silence_window(alert: Alert) -> timedelta:
    return SILENCE_WINDOW.get(alert.severity, timedelta(0))


def should_suppress(alert: Alert, recent: Sequence[Alert], now: datetime) -> bool:
    """
    静默窗口内，同 dedupe_key 的重复告警不再推送。

    P0 的窗口是 0，所以永远返回 False —— 这是刻意的。
    """
    window = silence_window(alert)
    if window <= timedelta(0):
        return False

    for past in recent:
        if past.dedupe_key != alert.dedupe_key:
            continue
        if past.created_at > now:
            continue
        if now - past.created_at <= window:
            return True
    return False


def overdue_for_ack(alert: Alert, now: datetime) -> bool:
    """需要确认、还没确认、且已经超过升级时限。"""
    if not requires_ack(alert) or alert.acknowledged:
        return False
    limit = ESCALATE_AFTER.get(alert.severity)
    if limit is None:
        return False
    return now - alert.created_at >= limit


def next_channels(
    alert: Alert,
    now: datetime,
    already_sent: Iterable[str] = (),
) -> tuple[str, ...]:
    """
    这一轮该补发哪些通道。

    已确认 → 空。不需要确认 → 空。没到升级时限 → 空。
    否则返回升级通道里还没发过的那些。
    """
    if not overdue_for_ack(alert, now):
        return ()
    sent = set(already_sent)
    return tuple(c for c in ESCALATION_CHANNELS.get(alert.severity, ()) if c not in sent)


def render(alert: Alert) -> str:
    tag = {"P0": "【紧急】", "P1": "【预警】", "P2": "【提示】"}.get(alert.severity, "【通知】")
    head = f"{tag}{alert.title}"
    if alert.detail:
        return f"{head}\n{alert.detail}"
    return head


def build_payload(alert: Alert, channel: str, recipient: str) -> dict:
    text = render(alert)
    if channel == Channel.PHONE:
        return {"type": "voice_call", "to": recipient, "text": text,
                "retry": 3, "severity": alert.severity}
    if channel == Channel.IM:
        return {"type": "im_text", "to": recipient, "msg_type": "text",
                "content": {"text": text}, "severity": alert.severity}
    if channel == Channel.EMAIL:
        return {"type": "email", "to": recipient, "subject": alert.title, "body": text}
    return {"type": "digest_item", "to": recipient, "severity": alert.severity,
            "title": alert.title, "detail": alert.detail}


def build_digest(alerts: Sequence[Alert], recipient: str = "") -> dict:
    """P2 攒到日报一起发。"""
    items = [{"severity": a.severity, "kind": a.kind, "store_id": a.store_id,
              "title": a.title, "detail": a.detail} for a in alerts]
    return {
        "type": "digest",
        "to": recipient,
        "count": len(items),
        "items": items,
        "text": "\n".join(render(a) for a in alerts) or "今日无提示类告警",
    }


# ===========================================================================
# 三、值班表
# ===========================================================================

@dataclass(frozen=True)
class Shift:
    person: str
    start: datetime
    end: datetime

    def covers(self, now: datetime) -> bool:
        return self.start <= now < self.end


def on_call(shifts: Iterable[Shift], now: datetime) -> Optional[str]:
    for shift in shifts:
        if shift.covers(now):
            return shift.person
    return None


def next_on_call(shifts: Iterable[Shift], now: datetime) -> Optional[str]:
    """当前没人值班时，下一个接班的 —— 用于告警里带上"下一个是谁"。"""
    upcoming = sorted((s for s in shifts if s.start > now), key=lambda s: s.start)
    return upcoming[0].person if upcoming else None


def coverage_gaps(
    shifts: Iterable[Shift],
    window_start: datetime,
    window_end: datetime,
) -> list[tuple[datetime, datetime]]:
    """
    找出值班表里没人覆盖的时间段。

    P0 告警发出去没人接，多半不是"人没看"，而是排班本来就漏了。
    这个洞本身就是一条该报的告警 —— 所以要在推送之前先检出来。
    """
    ordered = sorted(shifts, key=lambda s: s.start)
    gaps: list[tuple[datetime, datetime]] = []
    cursor = window_start

    for shift in ordered:
        if shift.end <= window_start or shift.start >= window_end:
            continue
        if shift.start > cursor:
            gaps.append((cursor, min(shift.start, window_end)))
        cursor = max(cursor, shift.end)
        if cursor >= window_end:
            break

    if cursor < window_end:
        gaps.append((cursor, window_end))
    return gaps


# ===========================================================================
# 四、从各模块产物构造告警（鸭子类型，不引入跨模块依赖）
# ===========================================================================

def from_inventory_alert(item: Any, *, now: Optional[datetime] = None) -> Alert:
    kind = Kind.STOCK_OUT if item.level == "OUT" else Kind.STOCK_LOW
    detail = (
        f"剩余 {item.available} 条（阈值 {item.threshold}）"
        if item.level != "OUT"
        else "卡密已用尽，商品已自动下架"
    )
    return make_alert(
        severity=item.priority, kind=kind, store_id=item.store_id,
        title=f"{item.title} 库存告警", detail=detail, created_at=now,
    )


def from_reconcile_issue(issue: Any, *, store_id: str, now: Optional[datetime] = None) -> Alert:
    return make_alert(
        severity=issue.severity, kind=f"RECONCILE_{issue.kind}", store_id=store_id,
        title=f"订单对账异常 {issue.order_id}", detail=issue.detail,
        created_at=now, dedupe_key=f"{issue.kind}:{store_id}:{issue.order_id}",
    )


def from_ticket(ticket: Any, *, now: Optional[datetime] = None) -> Alert:
    return make_alert(
        severity=ticket.priority, kind=Kind.QUEUE_BREACH, store_id=ticket.store_id,
        title=f"人工工单待处理（{ticket.priority}）",
        detail="、".join(ticket.reasons) or "需人工介入",
        created_at=ticket.opened_at, dedupe_key=f"TICKET:{ticket.id}",
    )


def from_audit_entry(entry: Any, *, now: Optional[datetime] = None) -> Optional[Alert]:
    """只有高敏感动作才推给店主 —— 不是每条审计都要吵人。"""
    if not entry.needs_alert:
        return None
    return make_alert(
        severity=Severity.P1, kind=Kind.CRITICAL_CHANGE,
        store_id=entry.store_id or "",
        title=f"{entry.actor_id} 执行了 {entry.action}",
        detail="、".join(entry.fields) or entry.note,
        created_at=now, dedupe_key=f"AUDIT:{entry.action}:{entry.target_id}",
    )
