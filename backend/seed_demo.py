"""
演示数据播种脚本
===============

用途：
  - 本地 `uvicorn` 起服务后，立刻有一份可玩的店铺/商品/卡密/FAQ/订单数据；
  - `docker compose up` 起一套干净环境后，控制台能直接看到东西，方便验收。

用法：
  # 不传 MASTER_KEY 会自动生成一把并打印（后端必须用同一把，否则卡密解不开）
  python -m backend.seed_demo

  # 显式指定（种子与后端必须用同一把 MASTER_KEY）
  MASTER_KEY=<base64-32bytes> \
  DATABASE_URL=sqlite+aiosqlite:///./app.db \
  python -m backend.seed_demo

幂等：tenant `t1` 已存在则整体跳过，重复执行不会脏写。
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .auth import hash_password
from .crypto import (
    LocalKeyProvider,
    configure_provider,
    content_fingerprint,
    generate_master_key,
)
from .models import (
    Base,
    CardPool,
    FaqRule,
    Order,
    Product,
    Store,
    Tenant,
    User,
)

# 演示账号。登录：username=owner / password=demo1234
DEMO_USERNAME = "owner"
DEMO_PASSWORD = "demo1234"

CARD_PLAIN = "A7F2-9K3M-XQ81-2ZP4"
NOW = datetime.now(timezone.utc)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _stamp_alembic_head(url: str) -> None:
    """把库标记到 Alembic 基线，避免"建表"和"迁移"两套机制打架。

    为什么必须有这一步：`create_all` 只建表、**不写 `alembic_version`**。如果播种
    完后库里没有版本行，运维第一次执行 `alembic upgrade head` 会直接把基线迁移
    重放一遍，撞上已存在的表并报 `table already exists` —— 而这个人只是想升级。

    为什么要开线程：本模块已经在事件循环里跑（`_seed` 是 async），而
    `command.stamp` 内部是 `asyncio.run(...)`，直接在循环里调用会抛
    `asyncio.run() cannot be called from a running event loop`。丢到子线程里跑，
    子线程没有自己的循环，asyncio.run 才能正常创建并关闭一个。注意**不能**把
    现有循环传过去，那样 stamp 会挂在同一个循环上，和正在跑的会话互相干扰。

    失败不影响播种：Alembic 没装、ini 缺失、或库被锁住时，只打印一行提示。
    播种是给人「马上有数据可玩」用的，不该因为迁移工具的问题而整体失败。
    """
    def _run() -> None:
        try:
            from alembic import command
            from alembic.config import Config
        except ImportError:  # 只装了运行依赖、没装 alembic 的场景
            print("[seed] 未安装 alembic，跳过版本标记（后续迁移请手工 stamp head）")
            return

        ini = PROJECT_ROOT / "alembic.ini"
        if not ini.exists():
            print(f"[seed] 未找到 {ini}，跳过版本标记")
            return

        # env.py 从环境变量读 URL。改成进程级环境变量是安全的：这里在子线程里
        # 串行执行，且紧随其后的播种逻辑只用 url 参数，不再读 DATABASE_URL。
        os.environ["DATABASE_URL"] = url
        try:
            command.stamp(Config(str(ini)), "head")
        except Exception as exc:  # noqa: BLE001 - 版本标记失败不该拖垮播种
            print(f"[seed] alembic 版本标记失败，已跳过：{exc}")
        else:
            print("[seed] 已标记 alembic 版本为 head")

    import threading

    t = threading.Thread(target=_run, name="alembic-stamp", daemon=True)
    t.start()
    t.join(timeout=30)
    if t.is_alive():
        print("[seed] alembic 版本标记超时（30s），已跳过")


async def _seed(url: str, master_key: str) -> None:
    provider = LocalKeyProvider(master_key)
    configure_provider(provider)

    engine = create_async_engine(url, pool_pre_ping=True)
    async with engine.begin() as conn:
        # 建表。注意：这里**只建不存在的表**，不会修改已有表结构。
        # 改模型后请用 Alembic 迁移（见 migrations/ 与 docs/MIGRATIONS.md）。
        await conn.run_sync(Base.metadata.create_all)
    _stamp_alembic_head(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as db:
        exists = (await db.execute(select(Tenant.id).where(Tenant.id == "t1"))).scalar_one_or_none()
        if exists is not None:
            print("[seed] tenant t1 已存在，跳过播种（如需重置请换一个 DATABASE_URL）")
            await engine.dispose()
            return

        def card(product_id: str, store_id: str, plain: str) -> CardPool:
            # key_ref 用店铺 id：AAD 绑定后，卡密被误写进别店行里解不开
            return CardPool(
                tenant_id="t1",
                product_id=product_id,
                content_encrypted=provider.encrypt(plain, store_id),
                content_hash=content_fingerprint(plain),
            )

        db.add_all([
            Tenant(id="t1", name="演示团队"),

            User(id="u1", tenant_id="t1", username=DEMO_USERNAME,
                 password_hash=hash_password(DEMO_PASSWORD),
                 role="OWNER", store_ids="s1,s2", is_active=True),

            Store(id="s1", tenant_id="t1", name="会员卡券铺",
                  platform_account="dy_6640", owner_name="李四", status="ONLINE"),
            Store(id="s2", tenant_id="t1", name="数码优选店",
                  platform_account="dy_8821", owner_name="张三", status="ONLINE"),

            Product(id="p1", tenant_id="t1", store_id="s1", item_id="ITEM-1",
                    title="爱奇艺黄金会员 年卡", listed_price=128.0, min_price=99.0,
                    shipping_policy="虚拟发货", send_type="CARD_POOL",
                    bargain_ladder=[0.05, 0.12, 0.23], low_stock_threshold=2,
                    auto_pause_on_empty=True, is_active=True),
            Product(id="p2", tenant_id="t1", store_id="s2", item_id="ITEM-2",
                    title="网易云音乐 季卡", listed_price=45.0, min_price=35.0,
                    shipping_policy="虚拟发货", send_type="CARD_POOL",
                    bargain_ladder=[0.05, 0.12, 0.20], low_stock_threshold=2,
                    auto_pause_on_empty=True, is_active=True),

            FaqRule(id="f1", tenant_id="t1", product_id="p1",
                    question="多久发货", answer="支付成功后 5 秒内自动发卡密。"),
            FaqRule(id="f2", tenant_id="t1", product_id="p2",
                    question="怎么激活", answer="卡密在对应 App 的会员中心充值即可。"),

            # 两个店铺都备足卡密池，否则对应订单发货会触发"卡密池已空"进补偿队列
            card("p1", "s1", f"{CARD_PLAIN}-0"),
            card("p1", "s1", f"{CARD_PLAIN}-1"),
            card("p2", "s2", f"{CARD_PLAIN}-2"),
            card("p2", "s2", f"{CARD_PLAIN}-3"),

            Order(id="o1", tenant_id="t1", store_id="s1", product_id="p1",
                  platform_order_id="PL-1", buyer_id="b1", amount=128.0, status="PAID"),
            Order(id="o2", tenant_id="t1", store_id="s2", product_id="p2",
                  platform_order_id="PL-2", buyer_id="b2", amount=45.0, status="PAID"),
        ])
        await db.commit()
        print("[seed] 已写入演示数据：2 店铺 / 2 商品 / 2 FAQ / 每店 2 张卡密 / 2 已付订单")
        print(f"[seed] 演示账号：username={DEMO_USERNAME} password={DEMO_PASSWORD} role=OWNER")

    await engine.dispose()


def main() -> None:
    master = os.environ.get("MASTER_KEY")
    if not master:
        master = generate_master_key()
        print(f"[seed] 未设置 MASTER_KEY，已临时生成一把（后端也必须用同一把）：\n      MASTER_KEY={master}")
    os.environ["MASTER_KEY"] = master

    url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./app.db")
    asyncio.run(_seed(url, master))


if __name__ == "__main__":
    main()
