"""
多店铺智控平台 · 业务硬约束层

这一层负责三件"不能出错"的事，且全部不依赖大模型的自觉：

1. 租户隔离   —— 每个请求的数据库会话被强制绑定到单一 tenant
2. 底价保护   —— AI 给出的任何报价在落库前都要过一遍服务端校验
3. 幂等发货   —— 取卡密 / 标记已用 / 发送 三步在事务内完成，订单号唯一约束兜底

把这些从 prompt 里挪到代码里，是这套系统能不能上线的前提。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .crypto import content_fingerprint as _content_fingerprint
from .crypto import get_provider
from .models import CardPool, Product, ShipmentRecord, Store

# ===========================================================================
# 一、租户隔离
# ===========================================================================


@dataclass(frozen=True)
class TenantCtx:
    """请求上下文中的租户身份。所有仓储方法的第一个参数都必须是它。"""

    tenant_id: str
    user_id: str


async def get_tenant_ctx(
    x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")],
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
    authorization: Annotated[Optional[str], Header()] = None,
) -> TenantCtx:
    """
    从请求头解析租户身份。

    真实实现里这里应该校验 JWT，并从 token 的 claim 里取 tenant_id —— 
    **绝不要相信客户端传来的 X-Tenant-Id**，这里只是为了展示契约形状。
    """
    if not authorization:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少凭证")
    # claims = verify_jwt(authorization.removeprefix("Bearer ").strip())
    # return TenantCtx(tenant_id=claims["tid"], user_id=claims["sub"])
    return TenantCtx(tenant_id=x_tenant_id, user_id=x_user_id)


TenantDep = Annotated[TenantCtx, Depends(get_tenant_ctx)]


def scoped(statement, ctx: TenantCtx, model):
    """
    给任意查询套上租户过滤。约定：所有查询都走这个函数，不允许裸 select(model)。

    建议再加一道 PostgreSQL RLS：
        ALTER TABLE product ENABLE ROW LEVEL SECURITY;
        CREATE POLICY p_tenant ON product
          USING (tenant_id = current_setting('app.tenant_id', true));
    并在每个事务开始时 SET LOCAL app.tenant_id = :tid。
    """
    return statement.where(model.tenant_id == ctx.tenant_id)


async def load_owned_store(db: AsyncSession, ctx: TenantCtx, store_id: str) -> Store:
    """取店铺时必须校验归属，防止越权访问别人的店铺。"""
    stmt = scoped(select(Store).where(Store.id == store_id), ctx, Store)
    store = (await db.execute(stmt)).scalar_one_or_none()
    if store is None:
        # 刻意返回 404 而非 403，避免泄露"该 store_id 存在但不属于你"
        raise HTTPException(status.HTTP_404_NOT_FOUND, "店铺不存在")
    return store


# ===========================================================================
# 二、底价硬保护
# ===========================================================================


class AiDecision(BaseModel):
    """强约束大模型的输出结构。字段缺失或越界直接判为无效，重试。"""

    reply: str = Field(min_length=1, max_length=1000)
    offered_price: Optional[float] = None
    intent: str = Field(pattern="^(ENQUIRY|BARGAIN|DEAL|UNKNOWN)$")

    @field_validator("reply")
    @classmethod
    def no_contact_leak(cls, v: str) -> str:
        """防止 AI 把联系方式、外部链接说出去导致交易脱平台。"""
        banned = ("微信", "加我", "VX", "vx", "qq", "QQ", "支付宝转账", "http://", "https://")
        if any(b in v for b in banned):
            raise ValueError("回复包含站外联系方式或链接")
        return v


# 报价越界后最多重试几次；仍失败就退回无报价的兜底话术
MAX_OFFER_RETRIES = 1
FALLBACK_REPLY = "这个价格实在做不了哈，已经是我能给的最低了，您看可以的话就直接拍～"


@dataclass(frozen=True)
class OfferBand:
    """某一轮议价中，服务端允许 AI 报价的闭区间。"""

    low: float
    high: float

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high


def offer_band(
    *,
    listed_price: float,
    min_price: float,
    ladder: list[float],
    round_no: int,
) -> OfferBand:
    """
    算出本轮允许的报价区间。

    ladder 是每轮最大让步比例，例如 [0.05, 0.12, 0.23]：
      第 1 轮最低可报 listed * 0.95，第 2 轮 listed * 0.88，第 3 轮 listed * 0.77。
    但无论阶梯怎么走，**下限都会被 min_price 兜住**。
    """
    if min_price > listed_price:
        raise ValueError("配置错误：底价高于挂牌价")

    if ladder:
        idx = min(max(round_no - 1, 0), len(ladder) - 1)
        pct = max(0.0, min(1.0, float(ladder[idx])))
    else:
        pct = 0.0

    low = listed_price * (1.0 - pct)
    low = max(low, min_price)  # 阶梯永不允许穿透底价
    return OfferBand(low=round(low, 2), high=round(listed_price, 2))


def clamp_offer(
    *,
    listed_price: float,
    min_price: float,
    ladder: list[float],
    round_no: int,
    ai_offered: Optional[float],
) -> Optional[float]:
    """
    校验 AI 的报价是否落在本轮合法区间内。

    返回 None 表示**不合法，必须重新生成整条决策**。

    这里刻意不把越界价格"夹"回区间内。原因是 reply 文本里通常已经写明了金额，
    只改价格会造成"文本说 90、实际报 95"的自相矛盾——这种不一致比直接拒绝更危险，
    买家截图投诉时你没法解释。所以越界一律判无效，让模型重来一次。
    """
    if ai_offered is None:
        return None

    band = offer_band(
        listed_price=listed_price, min_price=min_price, ladder=ladder, round_no=round_no
    )
    if not band.contains(float(ai_offered)):
        return None
    return round(float(ai_offered), 2)


async def evaluate_bargain(
    db: AsyncSession,
    ctx: TenantCtx,
    *,
    product_id: str,
    round_no: int,
    ai_decision: AiDecision,
) -> Optional[float]:
    """
    落库前的最后一道校验。AI 返回的价格只有通过这里才会写进数据库并说给买家。
    """
    stmt = scoped(select(Product).where(Product.id == product_id), ctx, Product)
    product = (await db.execute(stmt)).scalar_one_or_none()
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "商品不存在")

    if ai_decision.intent != "BARGAIN":
        return None

    safe = clamp_offer(
        listed_price=product.listed_price,
        min_price=product.min_price,
        ladder=product.bargain_ladder or [],
        round_no=round_no,
        ai_offered=ai_decision.offered_price,
    )
    if safe is None:
        # 调用方按 MAX_OFFER_RETRIES 重试；仍失败则改用 FALLBACK_REPLY（不带报价）
        return None
    return safe


# ===========================================================================
# 三、幂等自动发货
# ===========================================================================


@dataclass
class ShipResult:
    ok: bool
    card_content: Optional[str] = None
    reason: Optional[str] = None


async def claim_card_and_ship(
    db: AsyncSession,
    ctx: TenantCtx,
    *,
    store_id: str,
    product_id: str,
    order_id: str,
    buyer_id: str,
) -> ShipResult:
    """
    原子化发货。

    并发安全依赖两点：
      a) SELECT ... FOR UPDATE SKIP LOCKED —— 两个并发请求不会抢到同一张卡
      b) shipment_record.order_id 唯一约束 —— 同一订单重复回调时第二次会撞约束

    调用方负责把整个函数包在一个事务里；函数本身不 commit。
    """
    await load_owned_store(db, ctx, store_id)

    # --- 幂等闸门：该订单是否已经发过货 ---
    dup = await db.execute(
        select(ShipmentRecord).where(ShipmentRecord.order_id == order_id)
    )
    existing = dup.scalar_one_or_none()
    if existing is not None:
        return ShipResult(ok=True, reason=f"订单已处理（{existing.status}），跳过")

    stmt = scoped(select(Product).where(Product.id == product_id), ctx, Product)
    product = (await db.execute(stmt)).scalar_one_or_none()
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "商品不存在")

    record = ShipmentRecord(
        tenant_id=ctx.tenant_id,
        store_id=store_id,
        product_id=product_id,
        order_id=order_id,
        buyer_id=buyer_id,
        status="PENDING",
    )
    db.add(record)
    try:
        await db.flush()  # 让唯一约束在此刻生效，抢锁失败的一方会在这里炸
    except IntegrityError:
        await db.rollback()
        return ShipResult(ok=True, reason="订单已处理（并发去重），跳过")

    if product.send_type == "NONE":
        record.status = "FAILED"
        record.error = "该商品为人工发货，未配置自动履约"
        return ShipResult(ok=False, reason=record.error)

    if product.send_type == "NETDISK":
        record.status = "SENT"
        record.shipped_at = datetime.now(timezone.utc)
        return ShipResult(ok=True, card_content=f"{product.netdisk_url} 提取码 {product.netdisk_code}")

    if product.send_type != "CARD_POOL":
        record.status = "FAILED"
        record.error = f"未知发货类型 {product.send_type}"
        return ShipResult(ok=False, reason=record.error)

    # --- 卡密出库 ---
    # with_for_update(skip_locked=True) 在 PostgreSQL 上编译成 FOR UPDATE SKIP LOCKED，
    # 保证并发的两个请求不会抢到同一张卡；在 SQLite 上会被忽略（本来也没有并发）。
    # 用 ORM 而不是裸 SQL，是为了让这段逻辑能在 SQLite 上跑测试。
    card = (
        await db.execute(
            scoped(
                select(CardPool)
                .where(CardPool.product_id == product_id, CardPool.is_used.is_(False))
                .order_by(CardPool.created_at)   # 先进先出：先导入的先发
                .with_for_update(skip_locked=True)
                .limit(1),
                ctx,
                CardPool,
            )
        )
    ).scalar_one_or_none()

    if card is None:
        record.status = "FAILED"
        record.error = "卡密池已空，需要补货"
        return ShipResult(ok=False, reason=record.error)

    card.is_used = True
    card.used_at = datetime.now(timezone.utc)
    card.order_id = order_id

    record.card_id = card.id
    record.status = "SENT"
    record.shipped_at = datetime.now(timezone.utc)

    # 用店铺自己的密钥解密 —— key_ref 传 store_id，A 店的卡密在 B 店解不开
    plain = decrypt(card.content_encrypted, key_ref=store_id)
    return ShipResult(ok=True, card_content=plain)


def decrypt(cipher: str, key_ref: Optional[str] = None) -> str:
    """
    解密卡密。密钥服务见 crypto.py。

    key_ref 传店铺 ID。密文里已经写了它属于哪个 key_ref，所以传错会直接失败，
    而不是静默解出别家店的卡密 —— 这是刻意的。
    """
    return get_provider().decrypt(cipher, key_ref)


def encrypt(plain: str, key_ref: str) -> str:
    """卡密入库前加密。明文永远不要直接写进数据库。"""
    return get_provider().encrypt(plain, key_ref)


# 兼容旧调用点：指纹实现已挪到 crypto，这里保留转发
content_fingerprint = _content_fingerprint
