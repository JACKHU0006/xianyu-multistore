"""
捡漏监控：过滤链与多模态鉴真的成本控制

核心矛盾
--------
多模态鉴真（看图判断有没有暗伤、是不是二贩子文案）确实有用，但它**贵一个量级**：
一张图 1000+ token，比一整轮文本议价还贵。如果每个抓到的商品都送进去看，
一天的额度几小时就烧穿。

而抓到的商品里，绝大多数是垃圾 —— 价格不对、热度不够、跟关键词无关。
这些用**本地规则就能筛掉，成本为零**。

所以正确顺序是：

    抓取
      ↓
    ① 廉价过滤（关键词 / 价格区间 / 热度）     ← 本地，零成本，干掉 90%
      ↓
    ② 折扣计算（对比同款均价）                 ← 本地，零成本，干掉一大半
      ↓
    ③ 多模态鉴真                              ← 只在"折扣足够大"时才调
      ↓
    推送

`needs_vision()` 里的那条折扣门槛，是这套系统里性价比最高的一个判断 ——
它把最贵的资源只花在"真的可能是漏"的商品上。

`plan_filtering()` 是纯函数，可以穷举测试；真正的多模态调用由调用方注入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Protocol, Sequence, runtime_checkable

# 一次多模态鉴真的估算成本（元）。粗略值，接真实计费后应换成实际用量。
VISION_COST_PER_CALL = 0.02

# 默认每天最多调多少次多模态。宁可漏掉几个漏，也不能烧穿额度。
DEFAULT_VISION_BUDGET = 50


class Reject:
    PRICE = "PRICE_OUT_OF_RANGE"
    DESIRE = "LOW_DESIRE"
    KEYWORD = "KEYWORD_MISS"
    DUPLICATE = "DUPLICATE"
    STALE = "STALE"
    VISION_RISKY = "VISION_RISKY"


@dataclass(frozen=True)
class Listing:
    item_id: str
    title: str
    price: float
    desire_count: int = 0
    description: str = ""
    image_count: int = 0
    listed_at: Optional[datetime] = None
    # 多模态鉴真要拿图。只存 URL，不下载 —— 图片本身不进我们的存储，
    # 少一份副本就少一个泄露面。
    image_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class MonitorRule:
    keyword: str
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_desire: int = 0
    # 低于同款均价多少才值得调多模态。0.15 表示便宜 15% 以上才看
    vision_discount_threshold: float = 0.15
    vision_daily_budget: int = DEFAULT_VISION_BUDGET
    # 上架超过这么多小时的帖子不再考虑（捡漏要快）
    max_age_hours: Optional[int] = 72


@dataclass(frozen=True)
class Rejected:
    listing: Listing
    reason: str
    detail: str


@dataclass(frozen=True)
class VisionVerdict:
    risky: bool
    labels: tuple[str, ...] = ()
    confidence: float = 0.0
    note: str = ""


@runtime_checkable
class VisionVerifier(Protocol):
    """runtime_checkable 是为了能在装配时断言"这个实现真的满足协议"。"""

    async def verify(self, listing: Listing) -> VisionVerdict: ...


@dataclass(frozen=True)
class SourcingResult:
    accepted: tuple[Listing, ...]
    rejected: tuple[Rejected, ...]
    vision_calls: int
    vision_cost: float
    vision_skipped: int          # 因为折扣不够而跳过多模态的数量
    budget_exhausted: bool = False

    @property
    def scanned(self) -> int:
        return len(self.accepted) + len(self.rejected)

    @property
    def reject_breakdown(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rejected:
            out[r.reason] = out.get(r.reason, 0) + 1
        return out

    @property
    def vision_hit_rate(self) -> float:
        """调了多模态的里面，有多少真的被判为可疑 —— 越低说明门槛越松。"""
        return 0.0 if not self.vision_calls else round(
            sum(1 for r in self.rejected if r.reason == Reject.VISION_RISKY) / self.vision_calls, 4
        )


# ===========================================================================
# 一、廉价过滤（纯函数，零成本）
# ===========================================================================

def discount_ratio(price: float, median_price: float) -> float:
    """相对同款均价的折扣比例。0.2 表示便宜 20%。高于均价返回负数。"""
    if median_price <= 0:
        return 0.0
    return round((median_price - price) / median_price, 4)


def keyword_hit(listing: Listing, keyword: str) -> bool:
    """
    关键词命中：标题或描述里出现即可。

    这里刻意不做分词 —— 关键词本身就很短（"Switch OLED 日版"），
    拆开反而会误召回一堆无关商品。宁可漏一点，也别把噪音放进推送。
    """
    if not keyword:
        return True
    needle = keyword.casefold().strip()
    return needle in listing.title.casefold() or needle in listing.description.casefold()


def price_in_range(price: float, rule: MonitorRule) -> bool:
    if rule.min_price is not None and price < rule.min_price:
        return False
    if rule.max_price is not None and price > rule.max_price:
        return False
    return True


def is_stale(listing: Listing, rule: MonitorRule, now: datetime) -> bool:
    if rule.max_age_hours is None or listing.listed_at is None:
        return False
    listed = listing.listed_at if listing.listed_at.tzinfo else listing.listed_at.replace(
        tzinfo=timezone.utc)
    return (now - listed) > timedelta(hours=rule.max_age_hours)


def cheap_filter(
    listing: Listing,
    rule: MonitorRule,
    *,
    seen_item_ids: frozenset[str] = frozenset(),
    now: Optional[datetime] = None,
) -> Optional[Rejected]:
    """第一级过滤。返回 None 表示通过。"""
    moment = now or datetime.now(timezone.utc)

    if listing.item_id in seen_item_ids:
        return Rejected(listing, Reject.DUPLICATE, "该商品已推送过")
    if not keyword_hit(listing, rule.keyword):
        return Rejected(listing, Reject.KEYWORD, f"未命中关键词「{rule.keyword}」")
    if not price_in_range(listing.price, rule):
        return Rejected(listing, Reject.PRICE, f"价格 ¥{listing.price:g} 不在设定区间内")
    if listing.desire_count < rule.min_desire:
        return Rejected(listing, Reject.DESIRE,
                        f"仅 {listing.desire_count} 人想要，低于阈值 {rule.min_desire}")
    if is_stale(listing, rule, moment):
        return Rejected(listing, Reject.STALE, f"上架已超过 {rule.max_age_hours} 小时")
    return None


def needs_vision(listing: Listing, rule: MonitorRule, market_price: float) -> bool:
    """
    第二级：这个商品值不值得花多模态的钱。

    只对"折扣超过阈值"的商品才返回 True。这一条就是成本控制的核心 ——
    没有它，多模态会对着原价商品一张张看图，纯烧钱。
    """
    return discount_ratio(listing.price, market_price) >= rule.vision_discount_threshold


# ===========================================================================
# 二、过滤计划（纯函数）
# ===========================================================================

@dataclass(frozen=True)
class FilterPlan:
    to_verify: tuple[Listing, ...]        # 需要调多模态的
    no_vision_needed: tuple[Listing, ...] # 通过廉价过滤但折扣不够，直接可推
    rejected: tuple[Rejected, ...]
    budget_exhausted: bool = False

    @property
    def vision_skipped(self) -> int:
        return len(self.no_vision_needed)


def plan_filtering(
    listings: Iterable[Listing],
    rule: MonitorRule,
    market_price: float,
    *,
    seen_item_ids: frozenset[str] = frozenset(),
    vision_budget: Optional[int] = None,
    now: Optional[datetime] = None,
) -> FilterPlan:
    """
    算出这一批该怎么处理，但不真的调用任何外部服务。

    把"计划"和"执行"分开，好处是这个决策过程可以完全离线测试 ——
    不用 mock 任何网络请求就能验证"预算用完了会不会继续调"。
    """
    budget = rule.vision_daily_budget if vision_budget is None else vision_budget
    to_verify: list[Listing] = []
    no_vision: list[Listing] = []
    rejected: list[Rejected] = []
    exhausted = False

    for listing in listings:
        bad = cheap_filter(listing, rule, seen_item_ids=seen_item_ids, now=now)
        if bad is not None:
            rejected.append(bad)
            continue

        if not needs_vision(listing, rule, market_price):
            no_vision.append(listing)
            continue

        if len(to_verify) >= budget:
            # 预算用完了：不是丢掉，而是降级为"不看图直接推"。
            # 捡漏讲究时效，因为省 2 分钱错过一个漏不值得。
            exhausted = True
            no_vision.append(listing)
            continue

        to_verify.append(listing)

    return FilterPlan(
        to_verify=tuple(to_verify),
        no_vision_needed=tuple(no_vision),
        rejected=tuple(rejected),
        budget_exhausted=exhausted,
    )


# ===========================================================================
# 三、执行（需要注入多模态实现）
# ===========================================================================

async def run_sourcing(
    listings: Iterable[Listing],
    rule: MonitorRule,
    market_price: float,
    verifier: Optional[VisionVerifier] = None,
    *,
    seen_item_ids: frozenset[str] = frozenset(),
    vision_budget: Optional[int] = None,
    now: Optional[datetime] = None,
) -> SourcingResult:
    plan = plan_filtering(
        listings, rule, market_price,
        seen_item_ids=seen_item_ids, vision_budget=vision_budget, now=now,
    )

    accepted: list[Listing] = list(plan.no_vision_needed)
    rejected: list[Rejected] = list(plan.rejected)
    calls = 0

    if verifier is not None:
        for listing in plan.to_verify:
            verdict = await verifier.verify(listing)
            calls += 1
            if verdict.risky:
                rejected.append(Rejected(
                    listing, Reject.VISION_RISKY,
                    "、".join(verdict.labels) or verdict.note or "多模态判定有风险",
                ))
            else:
                accepted.append(listing)
    else:
        # 没有配置鉴真实现时，这些候选必须放行，不能静默丢掉。
        # 捡漏最怕的是漏掉真漏 —— 宁可推错几个让人自己看，也别让它们凭空消失。
        accepted.extend(plan.to_verify)

    return SourcingResult(
        accepted=tuple(accepted),
        rejected=tuple(rejected),
        vision_calls=calls,
        vision_cost=round(calls * VISION_COST_PER_CALL, 6),
        vision_skipped=plan.vision_skipped,
        budget_exhausted=plan.budget_exhausted,
    )


def estimate_daily_vision_cost(calls_per_day: int) -> float:
    return round(calls_per_day * VISION_COST_PER_CALL, 4)


def render_result(result: SourcingResult) -> str:
    lines = [
        f"扫描 {result.scanned} 条：通过 {len(result.accepted)}，"
        f"淘汰 {len(result.rejected)}",
    ]
    if result.reject_breakdown:
        detail = "、".join(f"{k} {v}" for k, v in sorted(result.reject_breakdown.items()))
        lines.append(f"  淘汰原因：{detail}")
    # 跳过有两种原因：折扣不够，或当日预算已用尽。措辞要覆盖两者，
    # 否则看报表的人会以为"预算够但没花"，从而去调错参数。
    lines.append(
        f"  多模态调用 {result.vision_calls} 次（成本约 ¥{result.vision_cost:.3f}），"
        f"跳过 {result.vision_skipped} 次（折扣不足或预算用尽）"
    )
    if result.budget_exhausted:
        lines.append("  ⚠ 多模态日预算已用尽，剩余商品降级为不鉴真直接推送")
    return "\n".join(lines)
