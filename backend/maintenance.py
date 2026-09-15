"""
维护任务：数据留存与重复率监控

两件事，都很容易被忽略，但都会在几个月后集中爆发。

一、留存策略
------------
消息表会一直涨。Supabase 免费额度 500MB，按 3 店每天 150 条算要 5 年才满 ——
听起来很久，但**查询变慢比存满更早发生**。一张几百万行的表，没有分区、
没有归档，翻一条三个月前的会话要好几秒，客服就不查了。

所以按表定不同的留存期：会话记录 90 天（够追溯纠纷），审计日志 2 年
（合规要求），完成的任务 7 天（纯噪音）。这不是"清理"，是**让表保持可用**。

二、重复率监控
--------------
这是最容易被浪费的一个指标。幂等去重本来只是为了防重复发货，但重复率本身
就是个**免费的故障探测器**：

    重复率突然从 2% 跳到 30% → 平台在重投消息 → 我们刚断过线

不需要额外埋点，不需要新增监控，数据本来就在那里。不看就白瞎了。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence


# ===========================================================================
# 一、留存策略
# ===========================================================================

# 每张表的保留天数。None 表示永久保留。
RETENTION_DAYS: dict[str, Optional[int]] = {
    "message_log": 90,        # 会话记录：够追溯纠纷就行
    "order_state_log": 365,   # 订单流转：一年，配合平台售后周期
    "shipment_record": 365,
    "audit_log": 730,         # 审计：两年，合规与内部追责都要
    "risk_event": 365,
    "task_queue": 7,          # 已完成的任务：纯噪音
    "orders": None,           # 订单本身永久保留（涉及资金）
}

# 各表每行的粗略占用（字节），含索引开销。用于估算容量。
BYTES_PER_ROW: dict[str, int] = {
    "message_log": 512,
    "order_state_log": 256,
    "audit_log": 768,
    "task_queue": 512,
    "orders": 384,
    "shipment_record": 320,
    "risk_event": 384,
}

DEFAULT_BYTES_PER_ROW = 512


@dataclass(frozen=True)
class ArchivePlan:
    table: str
    retention_days: Optional[int]
    cutoff: Optional[datetime]
    rows: int
    reason: str

    @property
    def actionable(self) -> bool:
        return self.cutoff is not None and self.rows > 0


def archive_cutoff(now: datetime, retention_days: int) -> datetime:
    if retention_days <= 0:
        raise ValueError("retention_days 必须为正数")
    return now - timedelta(days=retention_days)


def plan_retention(
    now: datetime,
    rows_by_table: dict[str, int],
    *,
    retention: Optional[dict[str, Optional[int]]] = None,
) -> tuple[ArchivePlan, ...]:
    """
    算出每张表该归档到什么时间点、预计影响多少行。

    先出计划再执行 —— 归档是不可逆操作，让人看一眼总量再动手，
    比脚本闷头删完再发现问题强。
    """
    policy = retention or RETENTION_DAYS
    plans: list[ArchivePlan] = []

    for table, rows in sorted(rows_by_table.items()):
        if table not in policy:
            plans.append(ArchivePlan(table, None, None, rows, "未配置留存策略，跳过"))
            continue

        days = policy[table]
        if days is None:
            plans.append(ArchivePlan(table, None, None, rows, "永久保留"))
            continue

        cutoff = archive_cutoff(now, days)
        plans.append(ArchivePlan(
            table, days, cutoff, rows,
            f"归档 {cutoff:%Y-%m-%d} 之前的数据（保留 {days} 天）",
        ))

    return tuple(plans)


def should_archive(created_at: datetime, now: datetime, retention_days: int) -> bool:
    moment = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    return moment < archive_cutoff(now, retention_days)


def estimate_table_mb(rows: int, table: str = "") -> float:
    per_row = BYTES_PER_ROW.get(table, DEFAULT_BYTES_PER_ROW)
    return round(rows * per_row / 1024 / 1024, 2)


def estimate_quota_pressure(
    rows_by_table: dict[str, int],
    quota_mb: float = 500.0,
) -> dict:
    """
    估算免费额度还剩多少。

    这里算的是"表本身"，不含索引膨胀与 WAL —— 真实占用通常比这个数高
    20%-40%，所以到 70% 就该动手，不要等 95%。
    """
    used = sum(estimate_table_mb(rows, t) for t, rows in rows_by_table.items())
    return {
        "used_mb": round(used, 2),
        "quota_mb": quota_mb,
        "used_ratio": round(used / quota_mb, 4) if quota_mb else 0.0,
        "should_act": used / quota_mb >= 0.7 if quota_mb else False,
        "by_table": {t: estimate_table_mb(r, t) for t, r in rows_by_table.items()},
    }


def render_retention_plan(plans: Sequence[ArchivePlan]) -> str:
    lines = ["留存计划："]
    for p in plans:
        mark = "→" if p.actionable else "·"
        lines.append(f"  {mark} {p.table:<18} {p.reason}")
    return "\n".join(lines)


# ===========================================================================
# 二、重复率监控
# ===========================================================================

class DedupLevel:
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    ALARMING = "ALARMING"


# 正常情况下重复率应该很低（偶尔的补推）。超过 5% 说明有问题，
# 超过 15% 基本可以确定刚经历过断线重连。
ELEVATED_THRESHOLD = 0.05
ALARMING_THRESHOLD = 0.15


@dataclass(frozen=True)
class DedupStats:
    total: int = 0
    duplicates: int = 0

    @property
    def duplicate_rate(self) -> float:
        return round(self.duplicates / self.total, 4) if self.total else 0.0

    @property
    def level(self) -> str:
        rate = self.duplicate_rate
        if rate >= ALARMING_THRESHOLD:
            return DedupLevel.ALARMING
        if rate >= ELEVATED_THRESHOLD:
            return DedupLevel.ELEVATED
        return DedupLevel.NORMAL

    @property
    def needs_attention(self) -> bool:
        return self.level != DedupLevel.NORMAL

    def diagnosis(self) -> str:
        rate = self.duplicate_rate
        if self.level == DedupLevel.ALARMING:
            return (
                f"重复率 {rate * 100:.1f}% 异常偏高，大概率刚发生过断线重连或平台补推。"
                "建议检查该店铺的长连接稳定性与心跳间隔"
            )
        if self.level == DedupLevel.ELEVATED:
            return f"重复率 {rate * 100:.1f}% 略高于常态，留意是否在缓慢恶化"
        return f"重复率 {rate * 100:.1f}%，正常"


def dedup_stats(outcomes: Iterable[object]) -> DedupStats:
    """从若干 DedupOutcome 汇总。只要对象有 total / duplicates 两个属性即可。"""
    total = duplicates = 0
    for outcome in outcomes:
        total += getattr(outcome, "total", 0)
        duplicates += len(getattr(outcome, "duplicates", ()) or ())
    return DedupStats(total=total, duplicates=duplicates)


def dedup_stats_from_counts(total: int, duplicates: int) -> DedupStats:
    return DedupStats(total=total, duplicates=duplicates)


def render_dedup(stats: DedupStats) -> str:
    return (
        f"消息去重：共 {stats.total} 条，重复 {stats.duplicates} 条"
        f"（{stats.duplicate_rate * 100:.1f}%）— {stats.diagnosis()}"
    )


# ===========================================================================
# 三、汇总
# ===========================================================================

def build_maintenance_report(
    now: datetime,
    rows_by_table: dict[str, int],
    dedup: DedupStats,
    *,
    quota_mb: float = 500.0,
) -> dict:
    plans = plan_retention(now, rows_by_table)
    quota = estimate_quota_pressure(rows_by_table, quota_mb)
    return {
        "retention": plans,
        "quota": quota,
        "dedup": dedup,
        "needs_attention": quota["should_act"] or dedup.needs_attention,
        "text": "\n\n".join([
            render_retention_plan(plans),
            f"容量：{quota['used_mb']}MB / {quota['quota_mb']}MB"
            f"（{quota['used_ratio'] * 100:.1f}%）"
            + ("，建议开始归档" if quota["should_act"] else ""),
            render_dedup(dedup),
        ]),
    }
