"""
多店铺智控平台 · 数据模型
SQLAlchemy 2.0 / PostgreSQL

设计原则
--------
1. 租户隔离：所有业务表都带 tenant_id。任何查询必须经过 tenant 作用域过滤，
   生产环境建议同时开启 PostgreSQL Row Level Security 作为第二道防线。
2. 店铺 = 一个真实闲鱼账号，归属唯一 tenant。店铺之间不共享凭据、不跨店复用会话。
3. 会话凭据只保存加密引用（vault_ref），明文由独立密钥服务持有，数据库不落地。
4. 不设计任何用于规避平台风控的字段（无代理池、无设备指纹、无验证码自动处理）。

迁移：alembic revision --autogenerate -m "init"
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String,
    Text, UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# 枚举（用 String + CHECK 约束表达，便于迁移）
# ---------------------------------------------------------------------------

class StoreStatus:
    ONLINE = "ONLINE"            # 监听正常
    NEEDS_HUMAN = "NEEDS_HUMAN"  # 需要店主本人完成身份验证，监听已挂起
    OFFLINE = "OFFLINE"          # 主动停用


class SendType:
    NONE = "NONE"                # 人工发货
    CARD_POOL = "CARD_POOL"      # 卡密池自动发货
    NETDISK = "NETDISK"          # 固定网盘链接


class Intent:
    ENQUIRY = "ENQUIRY"
    BARGAIN = "BARGAIN"
    DEAL = "DEAL"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# 租户与店铺
# ---------------------------------------------------------------------------

class Tenant(Base):
    """租户 = 一个店主或一个团队。所有数据的隔离边界。"""

    __tablename__ = "tenant"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    contact: Mapped[Optional[str]] = mapped_column(String(255))  # 通知用的 webhook / 邮箱
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    stores: Mapped[list["Store"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class User(Base):
    """
    登录账号。密码只存 PBKDF2 哈希，绝不落地明文。

    store_ids 用逗号分隔的字符串存（而非 JSON）是为了让"某用户能访问哪些店"
    这条最核心的授权判断保持简单可读；角色能力仍在 rbac.py 里集中定义。
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="VIEWER")
    store_ids: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "username", name="uq_user_tenant_username"),
    )


class Store(Base):
    """店铺 = 一个真实闲鱼账号。一个 tenant 可持有多个，但彼此完全独立。"""

    __tablename__ = "store"
    __table_args__ = (
        UniqueConstraint("tenant_id", "platform_account", name="uq_store_tenant_account"),
        Index("ix_store_tenant", "tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    platform_account: Mapped[str] = mapped_column(String(64), nullable=False)  # 闲鱼号，非敏感
    owner_name: Mapped[str] = mapped_column(String(64), nullable=False)        # 该店的实际负责人
    status: Mapped[str] = mapped_column(String(20), default=StoreStatus.OFFLINE, nullable=False)
    status_note: Mapped[Optional[str]] = mapped_column(String(255))

    # 自动化总开关（细粒度开关见 settings）
    auto_reply: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    auto_bargain: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    auto_ship: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    last_active_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="stores")
    session: Mapped[Optional["StoreSession"]] = relationship(
        back_populates="store", cascade="all, delete-orphan", uselist=False
    )
    products: Mapped[list["Product"]] = relationship(back_populates="store", cascade="all, delete-orphan")
    monitor_rules: Mapped[list["MonitorRule"]] = relationship(back_populates="store", cascade="all, delete-orphan")


class StoreSession(Base):
    """
    店铺登录态引用。

    注意：这里**不保存明文 Cookie**。vault_ref 指向外部密钥服务中的加密条目，
    且密钥服务对每个店铺使用独立密钥。控制台后端永远拿不到可直接复用的明文凭据，
    这是刻意的设计——它从根上排除了"凭据池化 / 跨店共享"的可能。
    """

    __tablename__ = "store_session"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    store_id: Mapped[str] = mapped_column(
        ForeignKey("store.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    vault_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    store: Mapped[Store] = relationship(back_populates="session")


# ---------------------------------------------------------------------------
# 商品 / 知识库 / 卡密
# ---------------------------------------------------------------------------

class Product(Base):
    __tablename__ = "product"
    __table_args__ = (
        UniqueConstraint("store_id", "item_id", name="uq_product_store_item"),
        Index("ix_product_tenant", "tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)  # 冗余，便于直接按租户过滤
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id", ondelete="CASCADE"), nullable=False)

    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    listed_price: Mapped[float] = mapped_column(Float, nullable=False)   # 挂牌价
    min_price: Mapped[float] = mapped_column(Float, nullable=False)      # 底价保护线，硬约束
    shipping_policy: Mapped[Optional[str]] = mapped_column(String(128))

    send_type: Mapped[str] = mapped_column(String(20), default=SendType.NONE, nullable=False)
    netdisk_url: Mapped[Optional[str]] = mapped_column(Text)
    netdisk_code: Mapped[Optional[str]] = mapped_column(String(32))

    # 议价策略：阶梯让步比例，例如 [0.06, 0.12, 0.23] 表示逐轮最多让挂牌价的百分比
    bargain_ladder: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # --- 库存管理 ---
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    auto_pause_on_empty: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    stock_paused_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    store: Mapped[Store] = relationship(back_populates="products")
    faqs: Mapped[list["FaqRule"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    cards: Mapped[list["CardPool"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class FaqRule(Base):
    """私有知识库。命中即直接回复，不调用大模型，省成本也省延迟。"""

    __tablename__ = "faq_rule"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("product.id", ondelete="CASCADE"), nullable=False)

    question: Mapped[str] = mapped_column(String(255), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    product: Mapped[Product] = relationship(back_populates="faqs")


class CardPool(Base):
    """卡密池。出库必须走 guardrails.claim_card_and_ship，禁止裸 UPDATE。"""

    __tablename__ = "card_pool"
    __table_args__ = (
        Index("ix_card_available", "product_id", "is_used"),
        UniqueConstraint("product_id", "content_hash", name="uq_card_product_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("product.id", ondelete="CASCADE"), nullable=False)

    content_encrypted: Mapped[str] = mapped_column(Text, nullable=False)  # 密文存储
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False) # 去重 / 唯一性校验
    is_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    order_id: Mapped[Optional[str]] = mapped_column(String(64))  # 出给了哪个订单
    # 出库按导入顺序先进先出，所以需要 created_at
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    product: Mapped[Product] = relationship(back_populates="cards")


# ---------------------------------------------------------------------------
# 监控 / 消息 / 履约 / 风控事件
# ---------------------------------------------------------------------------

class MonitorRule(Base):
    """捡漏监控规则。只做信息聚合与提醒，不代拍、不代付。"""

    __tablename__ = "monitor_rule"
    __table_args__ = (Index("ix_monitor_tenant", "tenant_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id", ondelete="CASCADE"), nullable=False)

    keyword: Mapped[str] = mapped_column(String(128), nullable=False)
    min_price: Mapped[Optional[float]] = mapped_column(Float)
    max_price: Mapped[Optional[float]] = mapped_column(Float)
    min_desire: Mapped[Optional[int]] = mapped_column(Integer)  # 「几人想要」下限
    ai_verify: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    store: Mapped[Store] = relationship(back_populates="monitor_rules")


class MessageLog(Base):
    __tablename__ = "message_log"
    __table_args__ = (
        Index("ix_msg_store_time", "store_id", "created_at"),
        Index("ix_msg_tenant", "tenant_id"),
        # 幂等去重的第二道防线：Redis 挂了也不会重复处理同一条平台消息。
        # Postgres 的 UNIQUE 允许多个 NULL，因此 AI/系统消息不受影响。
        UniqueConstraint("store_id", "platform_msg_id", name="uq_msg_store_platform"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id", ondelete="CASCADE"), nullable=False)

    platform_msg_id: Mapped[Optional[str]] = mapped_column(String(128))  # 平台消息 ID，幂等键
    fingerprint: Mapped[Optional[str]] = mapped_column(String(64))       # 无 ID 时的内容指纹
    buyer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False)  # BUYER / AI / SYSTEM
    content: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[Optional[str]] = mapped_column(String(16))
    offered_price: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShipmentRecord(Base):
    """
    履约记录。order_id 上的唯一约束是防重复发货的最后一道闸门。
    """

    __tablename__ = "shipment_record"
    __table_args__ = (UniqueConstraint("order_id", name="uq_shipment_order"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    card_id: Mapped[Optional[str]] = mapped_column(String(36))

    order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    buyer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)  # PENDING/SENT/FAILED
    error: Mapped[Optional[str]] = mapped_column(Text)
    shipped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Order(Base):
    """
    订单。表名用 orders 而不是 order —— order 是 SQL 保留字，别自找麻烦。

    platform_order_id + store_id 唯一：这是"一笔订单只能被记录一次"的根基，
    也是发货幂等的上游保证。
    """

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("store_id", "platform_order_id", name="uq_order_store_platform"),
        Index("ix_order_tenant_status", "tenant_id", "status"),
        Index("ix_order_paid_unshipped", "status", "paid_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[Optional[str]] = mapped_column(String(36))

    platform_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    buyer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="WAIT_BUYER_PAY", nullable=False)

    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    shipped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # 对账用：最后一次从平台同步到的状态，用来发现"本地与平台脱节"
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # lazy="selectin" 是必须的：AsyncSession 下惰性加载会抛 MissingGreenlet。
    # 状态流转要往这个集合里追加记录，所以它必须在加载 Order 时就一起就位。
    state_logs: Mapped[list["OrderStateLog"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )


class OrderStateLog(Base):
    """状态流转留痕。纠纷、赔付、审计都要靠它。"""

    __tablename__ = "order_state_log"
    __table_args__ = (Index("ix_osl_order", "order_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)

    from_status: Mapped[str] = mapped_column(String(20), nullable=False)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(255))
    actor: Mapped[Optional[str]] = mapped_column(String(64))  # SYSTEM / AI / 操作人
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped[Order] = relationship(back_populates="state_logs")


class RiskEvent(Base):
    """
    风控事件记录。

    只做三件事：记录 → 通知店主 → 挂起该店铺队列。
    处理方式固定为「店主本人在自己的设备上完成验证」，系统不参与验证过程本身。
    """

    __tablename__ = "risk_event"
    __table_args__ = (Index("ix_risk_store", "store_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id", ondelete="CASCADE"), nullable=False)

    kind: Mapped[str] = mapped_column(String(32), nullable=False)   # NEEDS_VERIFY / SESSION_EXPIRED
    detail: Mapped[Optional[str]] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[Optional[str]] = mapped_column(String(64))  # 操作人，留痕
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    """
    操作审计。

    这张表是**只追加**的：代码里不提供任何 update / delete 路径，
    数据库层面建议再加一条 REVOKE UPDATE, DELETE ON audit_log FROM app_user，
    让"改不掉"成为物理事实而不是约定。

    团队多店场景下没有这个，出了事就只能靠猜：底价是谁改的？卡密是谁导出的？
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_tenant_time", "tenant_id", "created_at"),
        Index("ix_audit_target", "target_type", "target_id"),
        Index("ix_audit_actor", "actor_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[Optional[str]] = mapped_column(String(36))

    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)

    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # [{"field": "min_price", "before": 99.0, "after": 89.0, "critical": true}]
    changes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    ip: Mapped[Optional[str]] = mapped_column(String(64))
    user_agent: Mapped[Optional[str]] = mapped_column(String(255))
    note: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskRecord(Base):
    """
    补偿任务持久化。

    放在数据库里而不是只放内存，是因为"进程重启"恰好是最需要补偿的时刻 ——
    如果任务只在内存里，一次发版就能把所有待重试的发货任务清空。
    """

    __tablename__ = "task_queue"
    __table_args__ = (
        Index("ix_task_due", "state", "next_run_at"),
        Index("ix_task_tenant", "tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state: Mapped[str] = mapped_column(String(12), default="PENDING", nullable=False)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RuleTemplate(Base):
    """租户级规则模板。一改全改，这是"多店省事"的来源。"""

    __tablename__ = "rule_template"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"),
                                           nullable=False, unique=True)
    rules: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StoreRuleOverride(Base):
    """
    单店覆盖。

    rules 里**只存该店真正想覆盖的字段**，不存全量 —— 存全量的话，
    以后模板改了什么，这个店都会因为"自己也有值"而被挡住，
    继承就名存实亡了。
    """

    __tablename__ = "store_rule_override"
    __table_args__ = (UniqueConstraint("store_id", name="uq_override_store"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id", ondelete="CASCADE"), nullable=False)
    rules: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
