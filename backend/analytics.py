"""
运营分析与健康度指标

为什么运营分析要写进代码，而不是拉个 BI 看板
--------------------------------------------
因为这套系统里已经躺着数据了，只是没人看。三个地方尤其浪费：

  1. **幂等去重的重复率**。它本来只是"防重复发货"的副产品，但重复率突然升高
     只说明一件事：平台在重投消息 —— 也就是我们刚断过线。这是一个免费的
     故障探测器，不看就白瞎了。
  2. **FAQ 命中率**。它决定响应延迟（毫秒 vs 2-4 秒），是体验的关键杠杆。
     不盯着它，就没动力去沉淀 FAQ。
  3. **咨询→成交的漏斗**。哪个环节在漏水，看数字比拍脑袋快。

所以这个模块只做一件事：**把已有数据变成能立刻行动的结论**。
判定逻辑全部是纯函数，可以穷举测试 —— 运营结论算错了比没有结论更糟。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

# ===========================================================================
# 一、转化漏斗
# ===========================================================================

STAGE_ORDER: tuple[str, ...] = ("inquiries", "bargains", "orders", "paid", "repeats")

STAGE_LABELS: dict[str, str] = {
    "inquiries": "咨询",
    "bargains": "议价",
    "orders": "下单",
    "paid": "支付",
    "repeats": "复购",
}


@dataclass(frozen=True)
class Funnel:
    inquiries: int = 0
    bargains: int = 0
    orders: int = 0
    paid: int = 0
    repeats: int = 0

    def counts(self) -> tuple[int, ...]:
        return tuple(getattr(self, s) for s in STAGE_ORDER)

    def rates(self) -> dict[str, float]:
        """每一跳相对上一阶段的转化率。第一跳没有上游，返回 1.0。"""
        out: dict[str, float] = {}
        prev: Optional[int] = None
        for stage in STAGE_ORDER:
            current = getattr(self, stage)
            if prev is None:
                out[stage] = 1.0
            else:
                out[stage] = round(current / prev, 4) if prev else 0.0
            prev = current
        return out

    def overall_rate(self) -> float:
        """咨询到支付的整体转化率。"""
        return round(self.paid / self.inquiries, 4) if self.inquiries else 0.0

    def anomalies(self) -> tuple[str, ...]:
        """
        后一阶段比前一阶段还多 —— 数据一定有问题。

        常见成因：消息日志漏记、订单没关联到会话、或者统计窗口没对齐。
        不检查的话，漏斗图会画出一个"转化率 180%"的漂亮数字，
        然后所有人拿着它开会。
        """
        problems: list[str] = []
        counts = self.counts()
        for i in range(1, len(counts)):
            if counts[i] > counts[i - 1]:
                problems.append(
                    f"{STAGE_LABELS[STAGE_ORDER[i]]}({counts[i]}) 多于 "
                    f"{STAGE_LABELS[STAGE_ORDER[i-1]]}({counts[i-1]})"
                )
        return tuple(problems)

    def bottleneck(self) -> tuple[str, float]:
        """漏水最严重的那一跳。用来回答"先修哪儿"。"""
        rates = self.rates()
        worst_stage, worst_rate = STAGE_ORDER[0], 1.0
        for stage in STAGE_ORDER[1:]:
            if rates[stage] < worst_rate:
                worst_stage, worst_rate = stage, rates[stage]
        return worst_stage, worst_rate


def build_funnel(**counts: int) -> Funnel:
    unknown = set(counts) - set(STAGE_ORDER)
    if unknown:
        raise KeyError(f"未知漏斗阶段：{', '.join(sorted(unknown))}")
    return Funnel(**{k: int(v) for k, v in counts.items()})


def render_funnel(funnel: Funnel) -> str:
    rates = funnel.rates()
    lines = ["转化漏斗："]
    for stage in STAGE_ORDER:
        label = STAGE_LABELS[stage]
        count = getattr(funnel, stage)
        pct = f"{rates[stage] * 100:.1f}%" if stage != STAGE_ORDER[0] else "—"
        lines.append(f"  {label:<4} {count:>6}   上一跳转化 {pct}")
    lines.append(f"  整体转化率 {funnel.overall_rate() * 100:.1f}%")
    worst, rate = funnel.bottleneck()
    lines.append(f"  最大瓶颈：{STAGE_LABELS[worst]}（{rate * 100:.1f}%）")
    for problem in funnel.anomalies():
        lines.append(f"  ⚠ 数据异常：{problem}")
    return "\n".join(lines)


# ===========================================================================
# 二、商品诊断
# ===========================================================================

class Verdict:
    STAR = "STAR"                  # 明星：卖得好，加库存、加曝光
    PRICING = "PRICING"            # 定价问题：有人问没人买
    NEGOTIATION = "NEGOTIATION"    # 议价僵持：砍价比例高但成交低，底价可能定高了
    TRAFFIC = "TRAFFIC"            # 曝光问题：几乎没人问
    STOCKOUT = "STOCKOUT"          # 缺货
    NORMAL = "NORMAL"


@dataclass(frozen=True)
class ProductStats:
    product_id: str
    title: str
    inquiries: int = 0
    bargains: int = 0
    orders: int = 0
    revenue: float = 0.0
    stock: int = 0
    days_listed: int = 0

    @property
    def conversion(self) -> float:
        return round(self.orders / self.inquiries, 4) if self.inquiries else 0.0

    @property
    def bargain_rate(self) -> float:
        """有多少比例的咨询进入了砍价。高说明买家觉得贵。"""
        return round(self.bargains / self.inquiries, 4) if self.inquiries else 0.0

    @property
    def avg_price(self) -> float:
        return round(self.revenue / self.orders, 2) if self.orders else 0.0


@dataclass(frozen=True)
class Diagnosis:
    product_id: str
    title: str
    verdict: str
    detail: str
    suggestion: str


# 样本太小时不下结论 —— "3 个人问过 0 个人买"不说明任何问题
MIN_INQUIRIES = 10
# 挂够多久还没人问，才算曝光问题
TRAFFIC_DAYS = 14

STAR_CONVERSION = 0.30
PRICING_CONVERSION = 0.05
NEGOTIATION_BARGAIN_RATE = 0.50
NEGOTIATION_CONVERSION = 0.15


def diagnose(stats: ProductStats, *, min_inquiries: int = MIN_INQUIRIES) -> Diagnosis:
    """
    判定顺序从"最确定的结论"到"最不确定"：

      缺货 > 明星 > 曝光不足 > 议价僵持 > 定价问题 > 正常

    顺序不能随便换。比如一个商品既没咨询又缺货，先说缺货才有意义 ——
    缺货时讨论"曝光不足"是浪费时间。
    """
    if stats.stock == 0 and stats.orders > 0:
        return Diagnosis(
            stats.product_id, stats.title, Verdict.STOCKOUT,
            "已售出但当前库存为 0",
            "立即补货或下架，避免买家拍下后无法交付",
        )

    if stats.orders >= min_inquiries // 2 and stats.conversion >= STAR_CONVERSION:
        return Diagnosis(
            stats.product_id, stats.title, Verdict.STAR,
            f"转化率 {stats.conversion * 100:.0f}%，成交 {stats.orders} 单",
            "加大曝光与库存，考虑提价测试利润空间",
        )

    if stats.inquiries < min_inquiries:
        if stats.days_listed >= TRAFFIC_DAYS:
            return Diagnosis(
                stats.product_id, stats.title, Verdict.TRAFFIC,
                f"上架 {stats.days_listed} 天仅 {stats.inquiries} 次咨询",
                "检查标题关键词、主图与定价区间，曝光可能不够",
            )
        return Diagnosis(
            stats.product_id, stats.title, Verdict.NORMAL,
            f"样本不足（{stats.inquiries} 次咨询，上架 {stats.days_listed} 天）",
            "继续观察",
        )

    if stats.bargain_rate >= NEGOTIATION_BARGAIN_RATE and stats.conversion < NEGOTIATION_CONVERSION:
        return Diagnosis(
            stats.product_id, stats.title, Verdict.NEGOTIATION,
            f"{stats.bargain_rate * 100:.0f}% 的咨询在砍价，但成交率仅 {stats.conversion * 100:.0f}%",
            "底价可能高于买家心理价位，或议价话术让得太慢",
        )

    if stats.conversion < PRICING_CONVERSION:
        return Diagnosis(
            stats.product_id, stats.title, Verdict.PRICING,
            f"{stats.inquiries} 次咨询仅成交 {stats.orders} 单",
            "定价或商品描述与买家预期不符，建议对比同款竞品",
        )

    return Diagnosis(
        stats.product_id, stats.title, Verdict.NORMAL,
        f"转化率 {stats.conversion * 100:.0f}%",
        "表现正常",
    )


def diagnose_all(
    items: Iterable[ProductStats], *, min_inquiries: int = MIN_INQUIRIES
) -> tuple[Diagnosis, ...]:
    """按"需要处理的紧急度"排序：缺货最急，其次明星（要加库存）。"""
    priority = {
        Verdict.STOCKOUT: 0, Verdict.STAR: 1, Verdict.PRICING: 2,
        Verdict.NEGOTIATION: 3, Verdict.TRAFFIC: 4, Verdict.NORMAL: 5,
    }
    out = [diagnose(i, min_inquiries=min_inquiries) for i in items]
    out.sort(key=lambda d: (priority.get(d.verdict, 9), d.product_id))
    return tuple(out)


# ===========================================================================
# 三、时段热度
# ===========================================================================

def hourly_heatmap(
    timestamps: Iterable[datetime],
    *,
    tz_offset_hours: int = 8,
) -> tuple[int, ...]:
    """
    把时间戳按小时分桶（默认东八区）。

    传入的时间戳应该是带时区的 UTC；用固定偏移转换而不是系统本地时区，
    是为了让结果在任何机器上跑都一致 —— 报表数字不该取决于服务器在哪。
    """
    buckets = [0] * 24
    offset = timedelta(hours=tz_offset_hours)
    for ts in timestamps:
        aware = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        buckets[(aware + offset).hour] += 1
    return tuple(buckets)


def peak_hours(heatmap: Sequence[int], top_n: int = 3) -> tuple[int, ...]:
    """最忙的几个小时。空数据返回空。"""
    if not any(heatmap):
        return ()
    ranked = sorted(range(len(heatmap)), key=lambda h: (-heatmap[h], h))
    return tuple(sorted(ranked[:top_n]))


def quiet_hours(
    heatmap: Sequence[int],
    *,
    ratio: float = 0.2,
) -> tuple[int, ...]:
    """
    咨询量低于峰值 ratio 的小时 —— 也就是"可以只留 AI、不用人盯"的时段。

    这个结论直接决定夜间排班。没有它，值班表只能靠感觉排，
    要么人不够，要么半夜三个人一起刷手机。
    """
    if not any(heatmap):
        return tuple(range(24))
    threshold = max(heatmap) * ratio
    return tuple(h for h, v in enumerate(heatmap) if v < threshold)


# ===========================================================================
# 四、健康度
# ===========================================================================

@dataclass(frozen=True)
class HealthSnapshot:
    faq_hit_rate: float = 0.0          # 越高越好，目标 >= 0.6
    ai_reply_share: float = 0.0        # 越高越好，目标 >= 0.8
    handoff_rate: float = 0.0          # 越低越好，目标 <= 0.2
    p0_open: int = 0                   # 越低越好，目标 0
    duplicate_rate: float = 0.0        # 越低越好，目标 <= 0.05
    stockout_products: int = 0         # 越低越好，目标 0


TARGETS: dict[str, tuple[str, float]] = {
    "faq_hit_rate": (">=", 0.60),
    "ai_reply_share": (">=", 0.80),
    "handoff_rate": ("<=", 0.20),
    "p0_open": ("<=", 0.0),
    "duplicate_rate": ("<=", 0.05),
    "stockout_products": ("<=", 0.0),
}

LABELS: dict[str, str] = {
    "faq_hit_rate": "FAQ 命中率",
    "ai_reply_share": "AI 自动处理占比",
    "handoff_rate": "转人工率",
    "p0_open": "未处理 P0 工单",
    "duplicate_rate": "重复消息率",
    "stockout_products": "缺货商品数",
}

# 每项不达标最多扣多少分。加起来超过 100 是有意的 —— 多项同时崩，
# 分数会直接归零，而不是"每项扣一点"显得还行。
WEIGHTS: dict[str, float] = {
    "faq_hit_rate": 15,
    "ai_reply_share": 15,
    "handoff_rate": 20,
    "p0_open": 30,
    "duplicate_rate": 25,
    "stockout_products": 20,
}


def _miss_ratio(name: str, value: float, target: float) -> float:
    """偏离目标的程度，0-1。1 表示偏得离谱。"""
    if name in ("p0_open", "stockout_products"):
        # 计数类：0 是达标，>0 按"有几个"线性扣，3 个封顶
        return min(1.0, value / 3.0)
    if target == 0:
        return min(1.0, value)
    if name == "faq_hit_rate":
        return min(1.0, max(0.0, (target - value) / target))
    # 越低越好的比率类：以目标为基准，超出一倍即满扣
    if value <= target:
        return 0.0
    return min(1.0, (value - target) / max(target, 0.01))


def evaluate_health(snapshot: HealthSnapshot) -> tuple[int, tuple[str, ...]]:
    """
    返回 (0-100 的健康分, 问题清单)。

    分数只是让人一眼看到"要不要管"，真正有用的是问题清单。
    """
    score = 100.0
    issues: list[str] = []

    for name, (op, target) in TARGETS.items():
        value = getattr(snapshot, name)
        ok = value >= target if op == ">=" else value <= target
        if ok:
            continue

        score -= WEIGHTS[name] * _miss_ratio(name, value, target)
        issues.append(f"{LABELS[name]} 当前 {value}，目标 {op} {target}")

    # 顺序固定，方便对比两次快照
    issues.sort(key=lambda s: -WEIGHTS[next(n for n in WEIGHTS if LABELS[n] in s)])
    return max(0, round(score)), tuple(issues)


def render_health(snapshot: HealthSnapshot) -> str:
    score, issues = evaluate_health(snapshot)
    head = f"健康分 {score}/100"
    if not issues:
        return head + "，各项正常"
    return head + "\n" + "\n".join("  · " + i for i in issues)


# ===========================================================================
# 五、周报
# ===========================================================================

@dataclass(frozen=True)
class WeeklyReport:
    period_start: datetime
    period_end: datetime
    funnel: Funnel
    revenue: float = 0.0
    ai_cost: float = 0.0
    health: HealthSnapshot = field(default_factory=HealthSnapshot)
    top_products: tuple[ProductStats, ...] = ()
    problems: tuple[Diagnosis, ...] = ()

    @property
    def gross_profit(self) -> float:
        return round(self.revenue - self.ai_cost, 4)

    @property
    def cost_ratio(self) -> float:
        """AI 成本占收入的比例。超过 5% 就该看看 FAQ 命中率了。"""
        return round(self.ai_cost / self.revenue, 4) if self.revenue else 0.0


def build_weekly_report(
    *,
    period_start: datetime,
    period_end: datetime,
    funnel: Funnel,
    revenue: float,
    ai_cost: float,
    health: HealthSnapshot,
    products: Sequence[ProductStats] = (),
) -> WeeklyReport:
    top = tuple(sorted(products, key=lambda p: (-p.revenue, p.product_id))[:5])
    problems = tuple(
        d for d in diagnose_all(products) if d.verdict != Verdict.NORMAL
    )
    return WeeklyReport(
        period_start=period_start, period_end=period_end, funnel=funnel,
        revenue=revenue, ai_cost=ai_cost, health=health,
        top_products=top, problems=problems,
    )


def render_weekly(report: WeeklyReport) -> str:
    span = f"{report.period_start:%Y-%m-%d} ~ {report.period_end:%Y-%m-%d}"
    lines = [
        f"周报（{span}）",
        "",
        render_funnel(report.funnel),
        "",
        f"收入 ¥{report.revenue:.2f}，AI 成本 ¥{report.ai_cost:.2f}"
        f"（占收入 {report.cost_ratio * 100:.2f}%）",
        "",
        render_health(report.health),
    ]
    if report.top_products:
        lines.append("")
        lines.append("收入 Top：")
        lines += [
            f"  {p.title}  ¥{p.revenue:.0f} / {p.orders} 单" for p in report.top_products
        ]
    if report.problems:
        lines.append("")
        lines.append("需要处理：")
        lines += [f"  [{d.verdict}] {d.title} —— {d.suggestion}" for d in report.problems]
    return "\n".join(lines)
