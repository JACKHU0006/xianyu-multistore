"""
操作审计

为什么必须有
------------
多店团队运营，一定会出现这三个问题，而且都只能靠审计日志回答：

  - 这个商品底价怎么从 99 变成 89 了？谁改的？
  - 卡密池里少了 200 条，是谁导出的？
  - 昨天那个账号的登录态是谁恢复的？

没有审计日志，这些问题只能靠猜，而猜错的代价是团队内讧。

两条硬规则
----------
1. **只记录真正变化的字段**。全量快照看着安全，实际会让日志被噪音淹没，
   真正要查的时候翻不出来。
2. **敏感字段的值一律打码**。审计日志本身绝不能变成凭据泄露的新渠道 ——
   Cookie、卡密明文、API Key 一旦写进日志，等于又多了一份泄露面。

`diff_changes()` 是纯函数，可以穷举测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .guardrails import TenantCtx, scoped
from .models import AuditLog


# ===========================================================================
# 一、动作与字段分级
# ===========================================================================

class Action:
    LOGIN = "LOGIN"
    STORE_CONNECT = "STORE_CONNECT"
    STORE_RESUME = "STORE_RESUME"
    STORE_DISABLE = "STORE_DISABLE"

    PRODUCT_CREATE = "PRODUCT_CREATE"
    PRODUCT_UPDATE = "PRODUCT_UPDATE"
    PRICE_UPDATE = "PRICE_UPDATE"
    MIN_PRICE_UPDATE = "MIN_PRICE_UPDATE"
    FAQ_UPDATE = "FAQ_UPDATE"

    CARD_IMPORT = "CARD_IMPORT"
    CARD_EXPORT = "CARD_EXPORT"
    CARD_REVEAL = "CARD_REVEAL"

    MONITOR_UPDATE = "MONITOR_UPDATE"
    TICKET_ASSIGN = "TICKET_ASSIGN"
    TICKET_CLOSE = "TICKET_CLOSE"
    USER_ROLE_CHANGE = "USER_ROLE_CHANGE"
    EXPORT_DATA = "EXPORT_DATA"


# 值一旦写进日志就等于二次泄露的字段（子串匹配，覆盖 cookie_string 之类）
SENSITIVE_HINTS = (
    "cookie", "token", "secret", "password", "passwd", "vault_ref",
    "api_key", "apikey", "credential", "card_content", "content_encrypted",
    "private_key", "session_id",
)

# 改了就影响钱或权限的字段 —— 需要单独标出来，方便过滤和告警
CRITICAL_FIELDS = frozenset({
    "min_price", "listed_price", "is_active", "send_type", "auto_send_type",
    "role", "store_ids", "auto_pause_on_empty", "bargain_ladder",
})

# 高敏感动作：不只是记录，还应该推送给店主
PRIVILEGED_ACTIONS = frozenset({
    Action.MIN_PRICE_UPDATE,
    Action.PRICE_UPDATE,
    Action.CARD_EXPORT,
    Action.CARD_REVEAL,
    Action.USER_ROLE_CHANGE,
    Action.STORE_RESUME,
    Action.STORE_DISABLE,
})

REDACTED = "***"


def is_sensitive(field_name: str) -> bool:
    f = field_name.casefold()
    return any(h in f for h in SENSITIVE_HINTS)


def is_critical(field_name: str) -> bool:
    return field_name.casefold() in CRITICAL_FIELDS


def is_privileged(action: str) -> bool:
    return action in PRIVILEGED_ACTIONS


# ===========================================================================
# 二、字段差异（纯函数）
# ===========================================================================

@dataclass(frozen=True)
class FieldChange:
    field: str
    before: Any
    after: Any

    @property
    def sensitive(self) -> bool:
        return is_sensitive(self.field)

    @property
    def critical(self) -> bool:
        return is_critical(self.field)

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "before": self.before,
            "after": self.after,
            "critical": self.critical,
        }


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def diff_changes(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    watch: Optional[Iterable[str]] = None,
) -> tuple[FieldChange, ...]:
    """
    算出 before → after 的字段级差异。

    - 只保留真正变化的字段（值相等就跳过，包括 99 和 99.0 这种）
    - 敏感字段一律打码，无论值是什么
    - watch 可以限定只关心某几个字段，避免把 updated_at 之类的噪音记进来
    """
    keys = set(watch) if watch is not None else (set(before) | set(after))
    changes: list[FieldChange] = []

    for key in sorted(keys):
        old = before.get(key)
        new = after.get(key)
        if old == new:
            continue
        if is_sensitive(key):
            changes.append(FieldChange(key, REDACTED, REDACTED))
        else:
            changes.append(FieldChange(key, _json_safe(old), _json_safe(new)))

    return tuple(changes)


def summarize(changes: Sequence[FieldChange]) -> str:
    if not changes:
        return "无字段变更"
    parts = []
    for c in changes:
        marker = "!" if c.critical else ""
        parts.append(f"{marker}{c.field}: {_fmt(c.before)} → {_fmt(c.after)}")
    return "；".join(parts)


def _fmt(value: Any) -> str:
    if value is None:
        return "空"
    if value is REDACTED:
        return REDACTED
    return str(value)


# ===========================================================================
# 三、审计条目
# ===========================================================================

@dataclass(frozen=True)
class AuditEntry:
    action: str
    tenant_id: str
    actor_id: str
    actor_role: str
    target_type: str
    target_id: str
    store_id: Optional[str] = None
    changes: tuple[FieldChange, ...] = ()
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    note: str = ""

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(c.field for c in self.changes)

    @property
    def has_critical_change(self) -> bool:
        return any(c.critical for c in self.changes)

    @property
    def needs_alert(self) -> bool:
        """要不要推给店主：高敏感动作，或者动了关键字段。"""
        return is_privileged(self.action) or self.has_critical_change

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "store_id": self.store_id,
            "actor_id": self.actor_id,
            "actor_role": self.actor_role,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "changes": [c.as_dict() for c in self.changes],
            "ip": self.ip,
            "note": self.note,
        }


def build_entry(
    *,
    action: str,
    ctx: TenantCtx,
    actor_role: str,
    target_type: str,
    target_id: str,
    store_id: Optional[str] = None,
    before: Optional[Mapping[str, Any]] = None,
    after: Optional[Mapping[str, Any]] = None,
    watch: Optional[Iterable[str]] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    note: str = "",
) -> AuditEntry:
    changes = ()
    if before is not None and after is not None:
        changes = diff_changes(before, after, watch=watch)
    return AuditEntry(
        action=action,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        actor_role=actor_role,
        target_type=target_type,
        target_id=target_id,
        store_id=store_id,
        changes=changes,
        ip=ip,
        user_agent=user_agent,
        note=note,
    )


def render(entry: AuditEntry) -> str:
    who = f"{entry.actor_id}({entry.actor_role})"
    what = f"{entry.target_type}#{entry.target_id}"
    detail = summarize(entry.changes) if entry.changes else (entry.note or "—")
    return f"{who} 对 {what} 执行 {entry.action}：{detail}"


# ===========================================================================
# 四、落库与查询
# ===========================================================================

async def persist(db: AsyncSession, entry: AuditEntry, model: Any = AuditLog) -> Any:
    """写入一条审计。调用方负责 commit。"""
    row = model(
        tenant_id=entry.tenant_id,
        store_id=entry.store_id,
        actor_id=entry.actor_id,
        actor_role=entry.actor_role,
        action=entry.action,
        target_type=entry.target_type,
        target_id=entry.target_id,
        changes=[c.as_dict() for c in entry.changes],
        ip=entry.ip,
        user_agent=entry.user_agent,
        note=entry.note or None,
    )
    db.add(row)
    await db.flush()
    return row


async def query(
    db: AsyncSession,
    ctx: TenantCtx,
    *,
    store_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    action: Optional[str] = None,
    target_id: Optional[str] = None,
    since: Optional[datetime] = None,
    only_critical: bool = False,
    limit: int = 200,
    model: Any = AuditLog,
) -> list[Any]:
    stmt = scoped(select(model), ctx, model)
    if store_id is not None:
        stmt = stmt.where(model.store_id == store_id)
    if actor_id is not None:
        stmt = stmt.where(model.actor_id == actor_id)
    if action is not None:
        stmt = stmt.where(model.action == action)
    if target_id is not None:
        stmt = stmt.where(model.target_id == target_id)
    if since is not None:
        stmt = stmt.where(model.created_at >= since)

    stmt = stmt.order_by(model.created_at.desc()).limit(limit)
    rows = list((await db.execute(stmt)).scalars().all())

    if only_critical:
        rows = [r for r in rows if any(c.get("critical") for c in (r.changes or []))]
    return rows


async def trail_for(
    db: AsyncSession,
    ctx: TenantCtx,
    target_type: str,
    target_id: str,
    *,
    limit: int = 50,
    model: Any = AuditLog,
) -> list[Any]:
    """某个对象的完整变更历史 —— 排查"这东西什么时候变成这样的"用这个。"""
    stmt = (
        scoped(select(model), ctx, model)
        .where(model.target_type == target_type, model.target_id == target_id)
        .order_by(model.created_at.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())
