"""
卡密库存监控与自动下架

为什么必须有这一层
------------------
虚拟商品的库存和实物商品不一样：实物卖超了还能跟买家商量，虚拟商品卖超了
就是"买家付了钱、你发不出货"，直接吃差评 + 平台赔付 + 店铺权重下降。

更隐蔽的风险是"静默卖超"：卡密池早就空了，但商品还在架上，AI 客服还在
正常接待、正常承诺"拍下秒发"。等到有人真的拍下才暴露，损失已经产生。

所以策略是：**主动扫描 + 阈值预警 + 归零自动下架**，不等人来发现。

三层阈值
--------
  OK  可用 > 阈值        正常
  LOW 0 < 可用 <= 阈值   预警，按预计可支撑天数决定优先级
  OUT 可用 == 0          立即下架 + P0 推送
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .guardrails import TenantCtx, scoped
from .models import CardPool, Product, ShipmentRecord

STOCK_OK = "OK"
STOCK_LOW = "LOW"
STOCK_OUT = "OUT"

DEFAULT_BURN_WINDOW_DAYS = 7


# ---------------------------------------------------------------------------
# 纯函数（可单测，不碰数据库）
# ---------------------------------------------------------------------------

def stock_level(available: int, threshold: int) -> str:
    if available <= 0:
        return STOCK_OUT
    if available <= threshold:
        return STOCK_LOW
    return STOCK_OK


def burn_rate(shipped_count: int, window_days: int = DEFAULT_BURN_WINDOW_DAYS) -> float:
    """日均消耗量。窗口期越短越灵敏，越长越平稳——7 天是个折中。"""
    if window_days <= 0:
        raise ValueError("window_days 必须为正数")
    return shipped_count / window_days


def days_of_cover(available: int, daily_burn: float) -> Optional[float]:
    """
    预计还能撑几天。

    返回 None 表示"算不出来"——没有任何消耗记录，说明这个商品还没开始卖，
    此时不应该报紧急。
    """
    if daily_burn <= 0:
        return None
    return available / daily_burn


def alert_priority(level: str, cover: Optional[float]) -> str:
    """
    通知优先级。

    关键点：库存不为零但"今天就要用完"的商品，优先级要和彻底断货一样高。
    只看 available 是否为零会漏掉这种最危险的情况。
    """
    if level == STOCK_OUT:
        return "P0"
    if cover is not None and cover < 1:
        return "P0"
    if level == STOCK_LOW:
        return "P1"
    return "P2"


def format_cover(cover: Optional[float]) -> str:
    if cover is None:
        return "暂无消耗数据"
    if cover < 1:
        return "不足 1 天"
    return f"约 {cover:.1f} 天"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class InventoryAlert:
    product_id: str
    store_id: str
    title: str
    level: str
    available: int
    threshold: int
    daily_burn: float
    cover_days: Optional[float]
    priority: str
    should_pause: bool
    paused_now: bool = False

    @property
    def is_actionable(self) -> bool:
        return self.level != STOCK_OK


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------

async def available_cards(db: AsyncSession, ctx: TenantCtx, product_id: str) -> int:
    stmt = scoped(
        select(func.count())
        .select_from(CardPool)
        .where(CardPool.product_id == product_id, CardPool.is_used.is_(False)),
        ctx,
        CardPool,
    )
    return int((await db.execute(stmt)).scalar_one())


async def shipped_in_window(
    db: AsyncSession,
    product_id: str,
    window_days: int = DEFAULT_BURN_WINDOW_DAYS,
) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    stmt = (
        select(func.count())
        .select_from(ShipmentRecord)
        .where(
            ShipmentRecord.product_id == product_id,
            ShipmentRecord.status == "SENT",
            ShipmentRecord.shipped_at >= since,
        )
    )
    return int((await db.execute(stmt)).scalar_one())


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------

async def scan_product(
    db: AsyncSession,
    ctx: TenantCtx,
    product: Product,
    *,
    window_days: int = DEFAULT_BURN_WINDOW_DAYS,
) -> Optional[InventoryAlert]:
    """只对卡密池类商品做库存扫描；人工发货和网盘商品没有库存概念。"""
    if product.send_type != "CARD_POOL":
        return None

    available = await available_cards(db, ctx, product.id)
    shipped = await shipped_in_window(db, product.id, window_days)
    burn = burn_rate(shipped, window_days)
    cover = days_of_cover(available, burn)
    level = stock_level(available, product.low_stock_threshold)

    return InventoryAlert(
        product_id=product.id,
        store_id=product.store_id,
        title=product.title,
        level=level,
        available=available,
        threshold=product.low_stock_threshold,
        daily_burn=round(burn, 2),
        cover_days=cover,
        priority=alert_priority(level, cover),
        should_pause=(level == STOCK_OUT and product.auto_pause_on_empty),
    )


async def scan_store(
    db: AsyncSession,
    ctx: TenantCtx,
    store_id: str,
    *,
    window_days: int = DEFAULT_BURN_WINDOW_DAYS,
) -> list[InventoryAlert]:
    stmt = scoped(
        select(Product).where(Product.store_id == store_id, Product.send_type == "CARD_POOL"),
        ctx,
        Product,
    )
    products = (await db.execute(stmt)).scalars().all()

    alerts: list[InventoryAlert] = []
    for product in products:
        alert = await scan_product(db, ctx, product, window_days=window_days)
        if alert is not None:
            alerts.append(alert)
    # 最紧急的排前面，方便值班的人从上往下看
    alerts.sort(key=lambda a: (a.priority, a.available))
    return alerts


# ---------------------------------------------------------------------------
# 自动下架 / 自动恢复
# ---------------------------------------------------------------------------

async def apply_auto_pause(db: AsyncSession, product: Product, alert: InventoryAlert) -> bool:
    """
    库存归零时把商品摘下来，避免"静默卖超"。

    返回 True 表示这次调用真的改变了状态（用于决定要不要推送通知，
    避免每分钟扫一次就推一次）。
    """
    if not alert.should_pause:
        return False
    if not product.is_active and product.stock_paused_at is not None:
        return False  # 已经因为缺货停过了

    product.is_active = False
    product.stock_paused_at = datetime.now(timezone.utc)
    alert.paused_now = True
    return True


async def resume_if_restocked(db: AsyncSession, product: Product, available: int) -> bool:
    """
    补货后自动恢复上架。

    只在"当初是因为缺货才下架"的情况下恢复——如果商品是运营手动下架的
    （stock_paused_at 为空），补货也不应该自动上架，那是人的决定。
    """
    if product.is_active or product.stock_paused_at is None:
        return False
    if available <= product.low_stock_threshold:
        return False

    product.is_active = True
    product.stock_paused_at = None
    return True


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------

def render_alert_text(alert: InventoryAlert, store_name: str = "") -> str:
    prefix = {"P0": "【紧急】", "P1": "【预警】", "P2": "【提示】"}[alert.priority]
    head = f"{prefix}{store_name + ' · ' if store_name else ''}{alert.title}"
    if alert.level == STOCK_OUT:
        body = f"卡密已用尽，商品{'已自动下架' if alert.paused_now else '需尽快下架'}，请立即补货。"
    else:
        body = (
            f"剩余 {alert.available} 条（阈值 {alert.threshold}），"
            f"日均消耗 {alert.daily_burn} 条，预计可支撑 {format_cover(alert.cover_days)}。"
        )
    return f"{head}\n{body}"


def build_webhook_payload(alerts: Sequence[InventoryAlert], store_names: dict[str, str] | None = None) -> dict:
    """构造飞书/钉钉通用文本卡片。发送动作交给 notifier，这里只出数据。"""
    store_names = store_names or {}
    actionable = [a for a in alerts if a.is_actionable]
    lines = [render_alert_text(a, store_names.get(a.store_id, "")) for a in actionable]
    return {
        "msg_type": "text",
        "content": {"text": "\n\n".join(lines) if lines else "库存全部正常"},
        "meta": {
            "count": len(actionable),
            "p0": sum(1 for a in actionable if a.priority == "P0"),
            "p1": sum(1 for a in actionable if a.priority == "P1"),
        },
    }
