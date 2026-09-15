"""
HTTP 接口层

把前面所有模块接成可用的服务。这一层刻意很薄 —— 每个接口都只做三件事：
  1. 校验权限（能力 + 数据范围）
  2. 调业务模块
  3. 返回结果

业务逻辑一行都不写在这里。写进来的话，这些逻辑就没法被单元测试覆盖了，
而它们恰恰是最容易出错的部分。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import analytics as analytics_mod
from . import audit as audit_mod
from . import auth as auth_mod
from . import inventory as inventory_mod
from . import maintenance as maintenance_mod
from . import orders as orders_mod
from . import rbac
from . import refunds as refunds_mod
from . import sourcing as sourcing_mod
from .guardrails import TenantCtx, claim_card_and_ship, scoped
from .handoff import ConvState, Ticket
from .handoff import queue_summary as handoff_queue_summary
from .handoff import render_queue as render_handoff_queue
from .idempotency import IdempotencyStore, IncomingMessage, MemoryIdempotencyStore
from .models import AuditLog, FaqRule, MessageLog, Order, Product, Store, TaskRecord, User
from .pipeline import Faq, ProductContext, Turn, UsageTracker, handle_turn
from .ratelimit import LimiterRegistry, RateLimitExceeded
from .retry import RetryQueue, TaskState

# 平台 webhook 适配器（懒加载，没配密钥时不影响启动）
from .adapters import AdapterError, SignatureMismatch, UnsupportedEvent, get_adapter

SHIP_TASK = "ship_order"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ===========================================================================
# 容器：把有状态的东西集中放一处
# ===========================================================================

@dataclass
class Container:
    session_factory: async_sessionmaker
    engine: Any = None                    # 持有 engine 是为了关停时能 dispose
    dedup: IdempotencyStore = field(default_factory=MemoryIdempotencyStore)
    usage: UsageTracker = field(default_factory=UsageTracker)
    retries: RetryQueue = field(default_factory=RetryQueue)
    limiters: LimiterRegistry = field(default_factory=LimiterRegistry)
    llm: Any = None                       # 未配置时管线会优雅降级
    vision: Any = None                    # 多模态鉴真，未配置时捡漏直接放行
    llm_bundle: Any = None                # 持有它才能在关停时释放连接池
    tickets: list[Ticket] = field(default_factory=list)
    # 平台级配置（如 webhook 签名密钥）。默认空 —— 未配时适配器放行并告警，
    # 方便开发；生产环境务必在环境变量里配上 <PLATFORM>_WEBHOOK_SECRET。
    settings: dict = field(default_factory=dict)

    # 鉴权。auth_mode="jwt" 时只认 Bearer token；"dev" 时退回 X-* 头（仅本地用）。
    jwt_secret: str = ""
    auth_mode: str = "dev"
    token_ttl_seconds: int = 12 * 3600

    # 消息级去重计数。这两个数加起来就是"重复消息率"——
    # 它不只是个统计，更是免费的断线探测器（见 maintenance.py）
    dedup_total: int = 0
    dedup_duplicates: int = 0


# ===========================================================================
# 请求/响应模型
# ===========================================================================

# ---------------------------------------------------------------------------
# 外部标识的长度上限，必须与 models.py 里的列宽一致
# ---------------------------------------------------------------------------
# 为什么要在入口就卡住：这些字段的值来自**平台推送**（webhook 原始报文、对账快照），
# 长度不受我们控制。而 SQLite 不校验 VARCHAR 长度，PostgreSQL 严格拒绝 ——
# 于是同一个超长 ID 在本地测试里一路绿灯，上生产却抛
# `StringDataRightTruncation` 变成 500，而且是**每条消息都 500**，直到有人发现。
# 在入参层返回 422 而不是让数据库报错，好处是：错误信息能指明是哪个字段、
# 并且不会把半截数据写进库。
#
# 注意这里是**拒绝**而不是截断：截断会把两个不同买家折叠成同一个 buyer_id，
# 对账和去重都会算错，属于静默的数据损坏，比报错更难查。
MSG_ID_MAX = 128     # message_log.platform_msg_id
BUYER_ID_MAX = 64    # message_log.buyer_id / orders.buyer_id / shipment_record.buyer_id
ORDER_ID_MAX = 64    # orders.platform_order_id / shipment_record.order_id
ITEM_ID_MAX = 64     # product.item_id
STORE_ID_MAX = 36    # store.id
PRODUCT_ID_MAX = 36  # product.id


class MessageIn(BaseModel):
    store_id: str = Field(min_length=1, max_length=STORE_ID_MAX)
    buyer_id: str = Field(min_length=1, max_length=BUYER_ID_MAX)
    content: str = Field(min_length=1, max_length=2000)
    msg_id: Optional[str] = Field(default=None, max_length=MSG_ID_MAX)
    item_id: Optional[str] = Field(default=None, max_length=ITEM_ID_MAX)
    round_no: int = Field(default=1, ge=1, le=20)
    unresolved_turns: int = Field(default=0, ge=0)
    conv_state: str = ConvState.AI


class OrderViewIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=ORDER_ID_MAX)
    status: str = Field(max_length=20)
    amount: float
    paid_at: Optional[datetime] = None
    shipped_at: Optional[datetime] = None


class ReconcileIn(BaseModel):
    # 快照来自平台对账接口，条数也不该由对方随意决定 —— 一次几千条会打满内存
    snapshot: list[OrderViewIn] = Field(default_factory=list, max_length=5000)


class RefundIn(BaseModel):
    reason: str = Field(min_length=1, max_length=255)
    delivered: bool
    card_revealed: bool = False
    duplicate_order: bool = False
    total_orders: int = Field(default=0, ge=0)
    refunds: int = Field(default=0, ge=0)
    off_platform_strikes: int = Field(default=0, ge=0)
    complaints: int = Field(default=0, ge=0)


class ProductStatsIn(BaseModel):
    product_id: str = Field(min_length=1, max_length=PRODUCT_ID_MAX)
    title: str = Field(max_length=255)  # product.title
    inquiries: int = Field(default=0, ge=0)
    bargains: int = Field(default=0, ge=0)
    orders: int = Field(default=0, ge=0)
    revenue: float = Field(default=0.0, ge=0)
    stock: int = Field(default=0, ge=0)
    days_listed: int = Field(default=0, ge=0)


class DiagnoseIn(BaseModel):
    products: list[ProductStatsIn] = Field(default_factory=list, max_length=2000)


class ListingIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=ITEM_ID_MAX)
    title: str = Field(max_length=255)
    price: float = Field(gt=0)
    desire_count: int = Field(default=0, ge=0)
    description: str = ""
    listed_at: Optional[datetime] = None


class SourcingIn(BaseModel):
    keyword: str = Field(min_length=1, max_length=128)  # monitor_rule.keyword
    market_price: float = Field(gt=0)
    listings: list[ListingIn] = Field(default_factory=list, max_length=5000)
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_desire: int = Field(default=0, ge=0)
    vision_discount_threshold: float = Field(default=0.15, ge=0, le=1)
    vision_daily_budget: int = Field(default=50, ge=0, le=500)


class ProductUpdateIn(BaseModel):
    """商品/规则的局部更新。只传要改的字段。"""

    min_price: Optional[float] = Field(default=None, gt=0)   # 底价：需额外权限
    listed_price: Optional[float] = Field(default=None, gt=0)
    low_stock_threshold: Optional[int] = Field(default=None, ge=0)
    is_active: Optional[bool] = None


class LoginIn(BaseModel):
    """登录请求。username 在多租户下可能重名，故可显式指定 tenant_id。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)
    tenant_id: Optional[str] = None


# ===========================================================================
# 依赖
# ===========================================================================

def build_dependencies(container: Container):
    async def get_db():
        async with container.session_factory() as db:
            yield db

    async def get_principal(
        authorization: Optional[str] = Header(None, alias="Authorization"),
        x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id"),
        x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
        x_role: str = Header("VIEWER", alias="X-Role"),
        x_store_ids: Optional[str] = Header(None, alias="X-Store-Ids"),
    ) -> rbac.Principal:
        """
        解析调用方身份。

        优先 `Authorization: Bearer <JWT>`。没有 Bearer 时，**仅当 auth_mode=="dev"**
        才退回 X-* 头（本地联调方便）；生产（AUTH_MODE=jwt）下没有 Bearer 一律 401。

        店铺范围规则：只有 MANAGER 允许不指定范围（= 看全部），其他人不写一律拒绝 ——
        "没写 = 全都能看"是最经典的越权来源。
        """
        token = auth_mod.bearer_token(authorization)
        if token is not None:
            try:
                claims = auth_mod.decode_token(token, container.jwt_secret)
            except auth_mod.TokenExpired as exc:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已过期，请重新登录") from exc
            except auth_mod.AuthError as exc:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效的登录凭证") from exc

            role = claims.get("role", rbac.Role.VIEWER)
            if role not in rbac.ALL_ROLES:
                raise HTTPException(status.HTTP_403_FORBIDDEN, f"未知角色 {role}")

            raw_scope = claims.get("store_ids") or []
            if isinstance(raw_scope, str):
                raw_scope = [s for s in raw_scope.split(",") if s.strip()]
            scope = frozenset(str(s) for s in raw_scope if str(s).strip())
            if not scope and role != rbac.Role.MANAGER:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "该账号未分配可访问的店铺范围")

            return rbac.Principal(
                user_id=str(claims.get("sub", "")),
                tenant_id=str(claims.get("tenant", "")),
                role=role,
                store_ids=scope or None,
            )

        # 没有 Bearer：只有 dev 模式才允许退回 X-* 头
        if container.auth_mode != "dev":
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "缺少 Authorization: Bearer 凭证",
            )
        if not x_tenant_id or not x_user_id:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "缺少身份头（dev 模式需 X-Tenant-Id / X-User-Id）",
            )
        if x_role not in rbac.ALL_ROLES:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"未知角色 {x_role}")

        if x_store_ids is None or not x_store_ids.strip():
            if x_role != rbac.Role.MANAGER:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "必须通过 X-Store-Ids 指定可访问的店铺范围",
                )
            dev_scope: Optional[frozenset[str]] = None
        else:
            dev_scope = frozenset(s.strip() for s in x_store_ids.split(",") if s.strip())
            if not dev_scope:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "店铺范围不能为空")

        return rbac.Principal(
            user_id=x_user_id, tenant_id=x_tenant_id, role=x_role, store_ids=dev_scope,
        )

    return get_db, get_principal


def _ctx(principal: rbac.Principal) -> TenantCtx:
    return TenantCtx(tenant_id=principal.tenant_id, user_id=principal.user_id)


def _guard(principal: rbac.Principal, perm: str, store_id: Optional[str] = None) -> None:
    try:
        rbac.check(principal, perm, store_id=store_id)
    except rbac.PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc


# ===========================================================================
# 路由
# ===========================================================================

def build_router(container: Container) -> APIRouter:
    get_db, get_principal = build_dependencies(container)
    router = APIRouter()

    # -- 健康检查 --------------------------------------------------------
    @router.get("/health", tags=["ops"])
    async def health() -> dict:
        return {
            "status": "ok",
            "time": _now().isoformat(),
            "retries": container.retries.stats(),
            "limiters": container.limiters.snapshot(),
        }

    # -- 登录（签发 JWT）-------------------------------------------------
    @router.post("/api/auth/login", tags=["auth"])
    async def login(body: LoginIn, db: AsyncSession = Depends(get_db)) -> dict:
        if not container.jwt_secret:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "服务端未配置 JWT_SECRET，无法签发登录凭证",
            )

        query = select(User).where(
            User.username == body.username, User.is_active.is_(True),
        )
        if body.tenant_id:
            query = query.where(User.tenant_id == body.tenant_id)
        users = list((await db.execute(query)).scalars().all())

        # 不区分"用户不存在"与"密码错误"，避免被当成用户名枚举接口
        if len(users) != 1 or not auth_mod.verify_password(body.password, users[0].password_hash):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")

        user = users[0]
        store_ids = [s for s in (user.store_ids or "").split(",") if s.strip()]
        token = auth_mod.create_token(
            {"sub": user.id, "tenant": user.tenant_id, "role": user.role,
             "store_ids": store_ids},
            container.jwt_secret,
            ttl_seconds=container.token_ttl_seconds,
        )
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": container.token_ttl_seconds,
            "role": user.role,
            "store_ids": store_ids,
        }

    # -- 当前身份 --------------------------------------------------------
    @router.get("/api/me", tags=["ops"])
    async def me(principal: rbac.Principal = Depends(get_principal)) -> dict:
        return {
            "user_id": principal.user_id,
            "tenant_id": principal.tenant_id,
            "role": principal.role,
            "tenant_wide": principal.tenant_wide,
            "stores": sorted(principal.store_ids) if principal.store_ids else None,
            "permissions": sorted(rbac.permissions_of(principal.role)),
        }

    # -- 店铺列表（按范围过滤） ------------------------------------------
    @router.get("/api/stores", tags=["store"])
    async def list_stores(
        principal: rbac.Principal = Depends(get_principal),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        _guard(principal, rbac.Perm.VIEW_DASHBOARD)
        ctx = _ctx(principal)

        stmt = scoped(select(Store), ctx, Store)
        stores = list((await db.execute(stmt)).scalars().all())

        # 关键：先按身份过滤，再返回。不能先返回再让前端藏。
        visible = rbac.visible_stores(principal, [s.id for s in stores])
        return {
            "items": [
                {
                    "id": s.id, "name": s.name, "account": s.platform_account,
                    "owner": s.owner_name, "status": s.status,
                    "auto_reply": s.auto_reply, "auto_ship": s.auto_ship,
                }
                for s in stores if s.id in visible
            ],
        }

    # -- 库存扫描 --------------------------------------------------------
    @router.get("/api/stores/{store_id}/inventory", tags=["store"])
    async def store_inventory(
        store_id: str,
        principal: rbac.Principal = Depends(get_principal),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        _guard(principal, rbac.Perm.VIEW_PRODUCT, store_id=store_id)
        alerts = await inventory_mod.scan_store(db, _ctx(principal), store_id)
        return {
            "store_id": store_id,
            "items": [
                {
                    "product_id": a.product_id, "title": a.title, "level": a.level,
                    "available": a.available, "threshold": a.threshold,
                    "daily_burn": a.daily_burn,
                    "cover_days": round(a.cover_days, 2) if a.cover_days else None,
                    "priority": a.priority, "should_pause": a.should_pause,
                }
                for a in alerts
            ],
        }

    # -- 订单对账 --------------------------------------------------------
    @router.post("/api/stores/{store_id}/reconcile", tags=["order"])
    async def store_reconcile(
        store_id: str,
        body: ReconcileIn,
        principal: rbac.Principal = Depends(get_principal),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        _guard(principal, rbac.Perm.VIEW_ORDER, store_id=store_id)
        ctx = _ctx(principal)
        now = _now()

        local = await orders_mod.load_local_views(db, ctx, store_id)
        issues = orders_mod.reconcile(
            local, {v.order_id: orders_mod.OrderView(**v.model_dump()) for v in body.snapshot}
        ) if body.snapshot else []

        stale = orders_mod.find_stale_unshipped(local.values(), now=now)
        all_issues = list(issues) + list(stale)

        sync = None
        if body.snapshot:
            result = await orders_mod.apply_snapshot(
                db, ctx, store_id,
                [orders_mod.OrderView(**v.model_dump()) for v in body.snapshot],
                now=now,
            )
            await db.commit()
            sync = {"created": result.created, "advanced": result.advanced,
                    "rejected": [i.detail for i in result.rejected]}

        stats = orders_mod.summarize(all_issues)
        return {
            "store_id": store_id,
            "summary": stats,
            "report": orders_mod.render_report(all_issues),
            "issues": [
                {"kind": i.kind, "severity": i.severity,
                 "order_id": i.order_id, "detail": i.detail}
                for i in all_issues
            ],
            "sync": sync,
        }

    # -- 消息入口 --------------------------------------------------------
    @router.post("/api/webhook/message", tags=["message"])
    async def webhook_message(
        body: MessageIn,
        principal: rbac.Principal = Depends(get_principal),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        _guard(principal, rbac.Perm.REPLY_MESSAGE, store_id=body.store_id)
        ctx = _ctx(principal)
        now = _now()

        # 限流：按店铺分桶，一个店打满不影响别的店
        try:
            container.limiters.get("send_message").acquire(body.store_id)
        except RateLimitExceeded as exc:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc

        product_ctx, faqs, product_id = await _load_product_context(db, ctx, body.store_id, body.item_id)

        turn = IncomingMessage(
            buyer_id=body.buyer_id, content=body.content,
            msg_id=body.msg_id, sent_at=now,
        )
        result = await handle_turn(
            turn,
            store_id=body.store_id,
            conv_state=body.conv_state,
            faqs=faqs,
            product=product_ctx,
            llm=container.llm,
            dedup_store=container.dedup,
            usage=container.usage,
            round_no=body.round_no,
            unresolved_turns=body.unresolved_turns,
            now=now,
        )

        container.dedup_total += 1
        if result.action == "DROP_DUPLICATE":
            container.dedup_duplicates += 1

        if result.ticket is not None:
            container.tickets.append(result.ticket)

        # 只有真正产生回复（或明确丢弃）的才落库，人工接管中的沉默不记账
        if result.action not in ("SILENT", "DROP_DUPLICATE"):
            db.add(MessageLog(
                tenant_id=ctx.tenant_id, store_id=body.store_id,
                platform_msg_id=body.msg_id,
                buyer_id=body.buyer_id, role="BUYER", content=body.content,
                intent=result.intent, offered_price=result.offered_price,
            ))
            if result.reply:
                db.add(MessageLog(
                    tenant_id=ctx.tenant_id, store_id=body.store_id,
                    buyer_id=body.buyer_id, role="AI", content=result.reply,
                    intent=result.intent,
                ))
            await db.commit()

        return {
            "action": result.action,
            "reply": result.reply,
            "draft_reply": result.draft_reply,
            "intent": result.intent,
            "offered_price": result.offered_price,
            "faq_score": result.faq_score,
            "cost": result.cost,
            "note": result.note,
            "blocked": bool(result.guard and result.guard.blocked),
            "guard_rules": list(result.guard.rules) if result.guard else [],
            "ticket": (
                {"id": result.ticket.id, "priority": result.ticket.priority,
                 "reasons": list(result.ticket.reasons), "score": result.ticket.score}
                if result.ticket else None
            ),
        }

    # -- 平台 webhook（闲鱼等）------------------------------------------
    @router.post("/api/webhook/{platform}", tags=["message"])
    async def platform_webhook(
        platform: str,
        request: Request,
        store_id: str = Query(...),
        principal: rbac.Principal = Depends(get_principal),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        """
        接收平台原始 webhook，经适配器解析后走内部管线。

        用法（以闲鱼为例）：
          POST /api/webhook/xianyu?store_id=s1
          Headers: X-Signature=xxx
          Body: {平台原始 JSON}
        """
        _guard(principal, rbac.Perm.REPLY_MESSAGE, store_id=store_id)

        try:
            adapter = get_adapter(
                platform,
                sign_secret=container.settings.get(f"{platform.upper()}_WEBHOOK_SECRET"),
            )
        except AdapterError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

        body = await request.body()
        try:
            msg = adapter.parse_message(body, dict(request.headers))
        except SignatureMismatch as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
        except UnsupportedEvent:
            # 不处理的事件类型返回 204，告诉平台别重试
            raise HTTPException(status.HTTP_204_NO_CONTENT, "事件类型不处理")
        except AdapterError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

        # 适配器不知道店铺归属，由 URL 参数补上
        msg_in = MessageIn(
            store_id=store_id,
            buyer_id=msg.buyer_id,
            content=msg.content,
            msg_id=msg.msg_id,
            item_id=msg.item_id,
        )

        # 复用内部消息处理逻辑
        return await webhook_message(msg_in, principal, db)

    # -- 商品/规则更新（写审计）-----------------------------------------
    @router.patch("/api/products/{product_id}", tags=["store"])
    async def update_product(
        product_id: str,
        body: ProductUpdateIn,
        store_id: str = Query(...),
        principal: rbac.Principal = Depends(get_principal),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        _guard(principal, rbac.Perm.EDIT_PRODUCT, store_id=store_id)
        ctx = _ctx(principal)

        product = (await db.execute(
            scoped(select(Product).where(
                Product.id == product_id, Product.store_id == store_id), ctx, Product)
        )).scalar_one_or_none()
        if product is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "商品不存在")

        updates = body.model_dump(exclude_unset=True, exclude_none=True)
        if not updates:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "没有需要更新的字段")

        # 改底价是更敏感的权限，单独再校验一次
        if "min_price" in updates:
            _guard(principal, rbac.Perm.EDIT_MIN_PRICE, store_id=store_id)

        # 底价不能高于挂牌价（否则议价区间会算错）
        new_min = updates.get("min_price", product.min_price)
        new_listed = updates.get("listed_price", product.listed_price)
        if new_min > new_listed:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "底价不能高于挂牌价")

        watch = list(updates.keys())
        before = {k: getattr(product, k) for k in watch}
        for k, v in updates.items():
            setattr(product, k, v)
        after = {k: getattr(product, k) for k in watch}

        action = (audit_mod.Action.MIN_PRICE_UPDATE if "min_price" in updates
                  else audit_mod.Action.PRODUCT_UPDATE)
        entry = audit_mod.build_entry(
            action=action, ctx=ctx, actor_role=principal.role,
            target_type="product", target_id=product_id, store_id=store_id,
            before=before, after=after, watch=watch,
            note=f"更新商品「{product.title}」",
        )
        row = await audit_mod.persist(db, entry)
        await db.commit()

        return {
            "product_id": product_id,
            "updated": updates,
            "audit_id": row.id,
            "action": entry.action,
            "changes": [c.as_dict() for c in entry.changes],
            "needs_alert": entry.needs_alert,
        }

    # -- 手动发货 --------------------------------------------------------
    @router.post("/api/orders/{order_id}/ship", tags=["order"])
    async def ship_order(
        order_id: str,
        store_id: str = Query(...),
        principal: rbac.Principal = Depends(get_principal),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        _guard(principal, rbac.Perm.SHIP, store_id=store_id)
        ctx = _ctx(principal)
        now = _now()

        order = (await db.execute(
            scoped(select(Order).where(
                Order.store_id == store_id, Order.platform_order_id == order_id), ctx, Order)
        )).scalar_one_or_none()
        if order is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "订单不存在")
        if not orders_mod.can_ship(order.status):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"订单状态为 {order.status}，不允许发货",
            )
        if not order.product_id:
            raise HTTPException(status.HTTP_409_CONFLICT, "订单未关联商品，无法自动发货")

        result = await claim_card_and_ship(
            db, ctx, store_id=store_id, product_id=order.product_id,
            order_id=order_id, buyer_id=order.buyer_id,
        )

        if result.ok:
            orders_mod.transition(order, orders_mod.OrderStatus.SHIPPED,
                                  reason="自动发货", actor="SYSTEM", now=now)
            await db.commit()
            return {"ok": True, "content": result.card_content, "retried": False}

        # 失败就进补偿队列，而不是直接返回错误让这笔订单自生自灭
        await db.commit()
        task = container.retries.enqueue(
            SHIP_TASK,
            {"order_id": order_id, "store_id": store_id, "product_id": order.product_id},
            now=now,
        )
        return {
            "ok": False,
            "reason": result.reason,
            "retry_task_id": task.id,
            "retry_at": task.next_run_at.isoformat() if task.next_run_at else None,
        }

    # -- 审计查询 --------------------------------------------------------
    @router.get("/api/audit", tags=["audit"])
    async def list_audit(
        store_id: Optional[str] = None,
        action: Optional[str] = None,
        only_critical: bool = False,
        limit: int = Query(100, ge=1, le=500),
        principal: rbac.Principal = Depends(get_principal),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        _guard(principal, rbac.Perm.VIEW_AUDIT)
        if store_id is not None:
            _guard(principal, rbac.Perm.VIEW_AUDIT, store_id=store_id)

        rows = await audit_mod.query(
            db, _ctx(principal), store_id=store_id, action=action,
            only_critical=only_critical, limit=limit,
        )
        return {
            "items": [
                {
                    "action": r.action, "actor": r.actor_id, "role": r.actor_role,
                    "store_id": r.store_id, "target": f"{r.target_type}#{r.target_id}",
                    "changes": r.changes, "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ],
        }

    # -- 人工队列 --------------------------------------------------------
    @router.get("/api/handoff/queue", tags=["handoff"])
    async def handoff_queue(
        principal: rbac.Principal = Depends(get_principal),
    ) -> dict:
        _guard(principal, rbac.Perm.VIEW_MESSAGES)
        now = _now()
        visible = [
            t for t in container.tickets
            if rbac.can_access_store(principal, t.store_id)
        ]
        summary = handoff_queue_summary(visible, now)
        return {"summary": summary, "text": render_handoff_queue(summary)}

    # -- 退款评估 --------------------------------------------------------
    @router.post("/api/refunds/evaluate", tags=["refund"])
    async def evaluate_refund(
        body: RefundIn,
        principal: rbac.Principal = Depends(get_principal),
    ) -> dict:
        _guard(principal, rbac.Perm.VIEW_ORDER)

        history = refunds_mod.BuyerHistory(
            total_orders=body.total_orders, refunds=body.refunds,
            off_platform_strikes=body.off_platform_strikes, complaints=body.complaints,
        )
        decision = refunds_mod.decide(
            body.reason,
            delivered=body.delivered,
            card_revealed=body.card_revealed,
            history=history,
            duplicate_order=body.duplicate_order,
        )
        return {
            "decision": decision.decision,
            "reason": decision.reason,
            "requires_proof": decision.requires_proof,
            "flag_buyer": decision.flag_buyer,
            "sla_minutes": decision.sla_minutes,
            "note": decision.note,
            "abuse_score": refunds_mod.abuse_score(history),
            "text": refunds_mod.render(decision),
        }

    # -- 运行指标 --------------------------------------------------------
    @router.get("/api/metrics", tags=["ops"])
    async def metrics(
        store_id: Optional[str] = None,
        principal: rbac.Principal = Depends(get_principal),
    ) -> dict:
        _guard(principal, rbac.Perm.VIEW_DASHBOARD)
        if store_id is not None:
            _guard(principal, rbac.Perm.VIEW_DASHBOARD, store_id=store_id)

        usage = (container.usage.report(store_id)
                 if store_id is not None
                 else container.usage.aggregate_report())
        tickets = [t for t in container.tickets
                   if store_id is None or t.store_id == store_id]
        return {
            "usage": usage,
            "retries": container.retries.stats(),
            "handoff": handoff_queue_summary(tickets, _now()),
            "limiters": container.limiters.snapshot(),
        }

    # -- 健康度 ----------------------------------------------------------
    @router.get("/api/analytics/health", tags=["analytics"])
    async def analytics_health(
        store_id: Optional[str] = None,
        principal: rbac.Principal = Depends(get_principal),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        _guard(principal, rbac.Perm.VIEW_DASHBOARD)
        if store_id is not None:
            _guard(principal, rbac.Perm.VIEW_DASHBOARD, store_id=store_id)

        usage = (container.usage.report(store_id)
                 if store_id is not None
                 else container.usage.aggregate_report())
        turns = usage["turns"] or 0
        tickets = [t for t in container.tickets if store_id is None or t.store_id == store_id]
        open_p0 = sum(1 for t in tickets if t.open and t.priority == "P0")

        stockouts = 0
        if store_id is not None:
            alerts = await inventory_mod.scan_store(db, _ctx(principal), store_id)
            stockouts = sum(1 for a in alerts if a.level == "OUT")

        # 注意：usage 是按天的，tickets 是容器生命周期的累计 —— 口径不完全一致。
        # 生产环境两者都应该从数据库按同一时间窗聚合，这里先用内存态近似。
        handoff_rate = min(1.0, round(len(tickets) / turns, 4)) if turns else 0.0
        dedup = maintenance_mod.DedupStats(container.dedup_total, container.dedup_duplicates)

        snapshot = analytics_mod.HealthSnapshot(
            faq_hit_rate=usage["faq_hit_rate"],
            ai_reply_share=round(max(0.0, 1.0 - handoff_rate), 4),
            handoff_rate=handoff_rate,
            p0_open=open_p0,
            duplicate_rate=dedup.duplicate_rate,
            stockout_products=stockouts,
        )
        score, issues = analytics_mod.evaluate_health(snapshot)

        return {
            "score": score,
            "issues": list(issues),
            "snapshot": asdict(snapshot),
            "usage": usage,
            "dedup": {
                "total": dedup.total, "duplicates": dedup.duplicates,
                "rate": dedup.duplicate_rate, "level": dedup.level,
                "diagnosis": dedup.diagnosis(),
            },
            "text": analytics_mod.render_health(snapshot),
        }

    # -- 商品诊断 --------------------------------------------------------
    @router.post("/api/analytics/diagnose", tags=["analytics"])
    async def analytics_diagnose(
        body: DiagnoseIn,
        principal: rbac.Principal = Depends(get_principal),
    ) -> dict:
        _guard(principal, rbac.Perm.VIEW_PRODUCT)
        items = [analytics_mod.ProductStats(**p.model_dump()) for p in body.products]
        return {
            "items": [
                {
                    "product_id": d.product_id, "title": d.title, "verdict": d.verdict,
                    "detail": d.detail, "suggestion": d.suggestion,
                }
                for d in analytics_mod.diagnose_all(items)
            ],
        }

    # -- 捡漏筛选 --------------------------------------------------------
    @router.post("/api/stores/{store_id}/sourcing", tags=["sourcing"])
    async def store_sourcing(
        store_id: str,
        body: SourcingIn,
        principal: rbac.Principal = Depends(get_principal),
    ) -> dict:
        _guard(principal, rbac.Perm.MANAGE_MONITOR, store_id=store_id)

        rule = sourcing_mod.MonitorRule(
            keyword=body.keyword,
            min_price=body.min_price,
            max_price=body.max_price,
            min_desire=body.min_desire,
            vision_discount_threshold=body.vision_discount_threshold,
            vision_daily_budget=body.vision_daily_budget,
        )
        listings = [
            sourcing_mod.Listing(
                item_id=i.item_id, title=i.title, price=i.price,
                desire_count=i.desire_count, description=i.description, listed_at=i.listed_at,
            )
            for i in body.listings
        ]

        # 配了 QWEN_API_KEY 就走真实多模态；没配则 verifier 为 None，
        # 该鉴真的候选会直接放行（不丢候选）—— 过滤链本身不用改。
        result = await sourcing_mod.run_sourcing(
            listings, rule, body.market_price,
            verifier=container.vision, now=_now())

        return {
            "accepted": [i.item_id for i in result.accepted],
            "rejected": [
                {"item_id": r.listing.item_id, "reason": r.reason, "detail": r.detail}
                for r in result.rejected
            ],
            "vision_calls": result.vision_calls,
            "vision_cost": result.vision_cost,
            "vision_skipped": result.vision_skipped,
            "budget_exhausted": result.budget_exhausted,
            "text": sourcing_mod.render_result(result),
        }

    # -- 维护报告 --------------------------------------------------------
    @router.get("/api/ops/maintenance", tags=["ops"])
    async def ops_maintenance(
        principal: rbac.Principal = Depends(get_principal),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        _guard(principal, rbac.Perm.VIEW_AUDIT)
        ctx = _ctx(principal)

        counts: dict[str, int] = {}
        for model, name in (
            (MessageLog, "message_log"), (AuditLog, "audit_log"),
            (TaskRecord, "task_queue"), (Order, "orders"),
        ):
            stmt = scoped(select(func.count()).select_from(model), ctx, model)
            counts[name] = int((await db.execute(stmt)).scalar_one())

        dedup = maintenance_mod.DedupStats(container.dedup_total, container.dedup_duplicates)
        report = maintenance_mod.build_maintenance_report(_now(), counts, dedup)

        return {
            "needs_attention": report["needs_attention"],
            "row_counts": counts,
            "quota": report["quota"],
            "dedup": {
                "total": dedup.total, "duplicates": dedup.duplicates,
                "rate": dedup.duplicate_rate, "level": dedup.level,
            },
            "retention": [
                {
                    "table": p.table, "retention_days": p.retention_days,
                    "actionable": p.actionable, "reason": p.reason,
                }
                for p in report["retention"]
            ],
            "text": report["text"],
        }

    return router


async def _load_product_context(
    db: AsyncSession,
    ctx: TenantCtx,
    store_id: str,
    item_id: Optional[str],
) -> tuple[Optional[ProductContext], list[Faq], Optional[str]]:
    if not item_id:
        return None, [], None

    product = (await db.execute(
        scoped(select(Product).where(
            Product.store_id == store_id, Product.item_id == item_id), ctx, Product)
    )).scalar_one_or_none()
    if product is None:
        return None, [], None

    faq_rows = list((await db.execute(
        scoped(select(FaqRule).where(FaqRule.product_id == product.id), ctx, FaqRule)
    )).scalars().all())

    context = ProductContext(
        title=product.title,
        listed_price=product.listed_price,
        min_price=product.min_price,
        ladder=tuple(product.bargain_ladder or ()),
        shipping_policy=product.shipping_policy or "",
    )
    return context, [Faq(q.question, q.answer) for q in faq_rows], product.id
