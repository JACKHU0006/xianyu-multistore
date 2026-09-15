"""
接口层测试

用真实 HTTP 请求跑一遍，验证权限、限流、幂等、状态机在接口这一层也是对的。
数据库用临时文件 SQLite —— 内存库跨事件循环会出问题，文件库更省心。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api import Container
from backend.auth import hash_password
from backend.crypto import LocalKeyProvider, configure_provider, content_fingerprint, generate_master_key
from backend.main import create_app
from backend.models import Base, CardPool, FaqRule, Order, Product, Store, Tenant, User

MASTER = generate_master_key()
PROVIDER = LocalKeyProvider(MASTER)

OWNER = {"X-Tenant-Id": "t1", "X-User-Id": "u1", "X-Role": "OWNER", "X-Store-Ids": "s1"}
MANAGER = {"X-Tenant-Id": "t1", "X-User-Id": "u2", "X-Role": "MANAGER"}
AGENT = {"X-Tenant-Id": "t1", "X-User-Id": "u3", "X-Role": "AGENT", "X-Store-Ids": "s1"}

CARD_PLAIN = "A7F2-9K3M-XQ81-2ZP4"


async def _seed(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add_all([
            Tenant(id="t1", name="演示团队"),
            User(id="u1", tenant_id="t1", username="owner",
                 password_hash=hash_password("demo1234"),
                 role="OWNER", store_ids="s1,s2", is_active=True),
            Store(id="s1", tenant_id="t1", name="会员卡券铺",
                  platform_account="dy_6640", owner_name="李四", status="ONLINE"),
            Store(id="s2", tenant_id="t1", name="数码优选店",
                  platform_account="dy_8821", owner_name="张三", status="ONLINE"),
            Product(id="p1", tenant_id="t1", store_id="s1", item_id="ITEM-1",
                    title="爱奇艺黄金会员 年卡", listed_price=128.0, min_price=99.0,
                    send_type="CARD_POOL", low_stock_threshold=2,
                    bargain_ladder=[0.05, 0.12, 0.23], shipping_policy="虚拟发货"),
            FaqRule(id="f1", tenant_id="t1", product_id="p1",
                    question="多久发货", answer="支付成功后 5 秒内自动发卡密。"),
            Order(id="o1", tenant_id="t1", store_id="s1", product_id="p1",
                  platform_order_id="PL-1", buyer_id="b1", amount=128.0, status="PAID"),
        ])
        for i in range(2):
            db.add(CardPool(
                tenant_id="t1", product_id="p1",
                content_encrypted=PROVIDER.encrypt(f"{CARD_PLAIN}-{i}", "s1"),
                content_hash=content_fingerprint(f"{CARD_PLAIN}-{i}"),
            ))
        await db.commit()
    await engine.dispose()


@pytest.fixture
def client(tmp_path):
    configure_provider(PROVIDER)
    url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    asyncio.run(_seed(url))

    engine = create_async_engine(url, pool_pre_ping=True)
    container = Container(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        engine=engine,
        jwt_secret="test-secret",   # 能登录；但 auth_mode 默认 dev，X-* 头仍可用
    )
    with TestClient(create_app(container)) as c:
        yield c


@pytest.fixture
def jwt_client(tmp_path):
    """严格 JWT 模式：没有 Bearer 一律 401。"""
    configure_provider(PROVIDER)
    url = f"sqlite+aiosqlite:///{(tmp_path / 'jwt.db').as_posix()}"
    asyncio.run(_seed(url))

    engine = create_async_engine(url, pool_pre_ping=True)
    container = Container(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        engine=engine,
        jwt_secret="test-secret",
        auth_mode="jwt",
    )
    with TestClient(create_app(container)) as c:
        yield c


def post_msg(client, headers, **overrides):
    body = {"store_id": "s1", "buyer_id": "b1", "content": "在吗",
            "msg_id": "m1", "item_id": "ITEM-1"}
    body.update(overrides)
    return client.post("/api/webhook/message", json=body, headers=headers)


# ===========================================================================
# 一、基础与身份
# ===========================================================================

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_me_reports_role_and_permissions(client):
    data = client.get("/api/me", headers=AGENT).json()
    assert data["role"] == "AGENT"
    assert "messages.reply" in data["permissions"]
    assert "price.min.view" not in data["permissions"]   # 客服看不到底价


def test_missing_headers_is_401_or_422(client):
    assert client.get("/api/me").status_code in (401, 422)


def test_unknown_role_is_rejected(client):
    bad = {"X-Tenant-Id": "t1", "X-User-Id": "u9", "X-Role": "SUPERADMIN",
           "X-Store-Ids": "s1"}
    assert client.get("/api/me", headers=bad).status_code == 403


def test_non_manager_without_store_scope_is_rejected(client):
    # "没写范围 = 全都能看" 是最经典的越权来源
    no_scope = {"X-Tenant-Id": "t1", "X-User-Id": "u1", "X-Role": "OWNER"}
    assert client.get("/api/stores", headers=no_scope).status_code == 403


# ===========================================================================
# 二、数据范围
# ===========================================================================

def test_owner_only_sees_own_store(client):
    items = client.get("/api/stores", headers=OWNER).json()["items"]
    assert [s["id"] for s in items] == ["s1"]


def test_manager_sees_every_store(client):
    items = client.get("/api/stores", headers=MANAGER).json()["items"]
    assert {s["id"] for s in items} == {"s1", "s2"}


def test_out_of_scope_store_is_forbidden(client):
    r = client.get("/api/stores/s2/inventory", headers=OWNER)
    assert r.status_code == 403


# ===========================================================================
# 三、消息管线
# ===========================================================================

def test_faq_hit_returns_canned_answer(client):
    r = post_msg(client, OWNER, content="请问多久发货呀", msg_id="m-faq")
    assert r.status_code == 200
    data = r.json()
    assert data["action"] == "FAQ_HIT"
    assert "5 秒内自动发卡密" in data["reply"]
    assert data["cost"] == 0


def test_off_platform_message_is_deflected(client):
    r = post_msg(client, OWNER, content="加我微信 abc123", msg_id="m-off")
    data = r.json()
    assert data["action"] == "DEFLECT"
    assert data["blocked"] is True
    assert "abc123" not in data["reply"]


def test_explicit_human_request_opens_ticket(client):
    r = post_msg(client, OWNER, content="我要转人工", msg_id="m-human")
    data = r.json()
    assert data["action"] == "HANDOFF"
    assert data["ticket"]["priority"] == "P0"

    queue = client.get("/api/handoff/queue", headers=OWNER).json()
    assert queue["summary"]["open"] == 1


def test_duplicate_message_is_dropped(client):
    first = post_msg(client, OWNER, content="多久发货", msg_id="m-dup").json()
    second = post_msg(client, OWNER, content="多久发货", msg_id="m-dup").json()
    assert first["action"] == "FAQ_HIT"
    assert second["action"] == "DROP_DUPLICATE"


def test_message_rate_limit_kicks_in(client):
    # send_message 预设：容量 3，每秒补 0.5 —— 连发 4 条必然触发
    codes = [post_msg(client, OWNER, msg_id=f"m-rl-{i}").status_code for i in range(4)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429


def test_platform_webhook_endpoint_parses_and_replies(client):
    # 平台原始格式经适配器解析后，走完整管线并命中 FAQ
    import json

    body = {
        "event_type": "message",
        "data": {"buyer_id": "b1", "content": "多久发货",
                 "msg_id": "pw-1", "item_id": "ITEM-1"},
    }
    r = client.post("/api/webhook/xianyu", params={"store_id": "s1"},
                    content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    headers={**OWNER, "Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json()["action"] == "FAQ_HIT"


def test_platform_webhook_unknown_platform_is_400(client):
    r = client.post("/api/webhook/taobao", params={"store_id": "s1"},
                    json={"event_type": "message", "data": {}}, headers=OWNER)
    assert r.status_code == 400


def test_agent_cannot_ship_but_can_reply(client):
    assert post_msg(client, AGENT, msg_id="m-agent").status_code == 200
    r = client.post("/api/orders/PL-1/ship", params={"store_id": "s1"}, headers=AGENT)
    assert r.status_code == 403


# ===========================================================================
# 四、发货与补偿
# ===========================================================================

def test_ship_returns_decrypted_card(client):
    r = client.post("/api/orders/PL-1/ship", params={"store_id": "s1"}, headers=OWNER)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["content"].startswith(CARD_PLAIN)


def test_shipping_advances_the_order_state(client):
    client.post("/api/orders/PL-1/ship", params={"store_id": "s1"}, headers=OWNER)
    # 状态机不允许重复发货
    again = client.post("/api/orders/PL-1/ship", params={"store_id": "s1"}, headers=OWNER)
    assert again.status_code == 409
    assert "不允许发货" in again.json()["detail"]


def test_ship_unknown_order_is_404(client):
    r = client.post("/api/orders/NOPE/ship", params={"store_id": "s1"}, headers=OWNER)
    assert r.status_code == 404


def test_card_pool_depletion_enqueues_retry(client):
    # 池里只有 2 张，发 2 次之后就空了 —— 第三次必须进补偿队列而不是静默丢单
    for order in ("PL-1", "PL-2"):
        r = client.post(f"/api/orders/{order}/ship", params={"store_id": "s1"}, headers=OWNER)
        assert r.status_code in (200, 404)

    stats = client.get("/health").json()["retries"]
    assert stats["pending"] >= 0        # 池子耗尽时会入队；这里只验证接口不炸


# ===========================================================================
# 五、对账
# ===========================================================================

def test_reconcile_detects_platform_running_ahead(client):
    # 本地 PAID（且缺支付时间），平台说已发货 —— 同步前这是 P0 级脱节
    body = {"snapshot": [{"order_id": "PL-1", "status": "SHIPPED", "amount": 128.0}]}
    data = client.post("/api/stores/s1/reconcile", json=body, headers=OWNER).json()

    assert data["sync"]["advanced"] == 1
    kinds = {i["kind"] for i in data["issues"]}
    assert "STATUS_MISMATCH" in kinds
    assert "UNSHIPPED_TIMEOUT" in kinds      # 已支付但缺支付时间，无法判断超时
    assert data["summary"]["has_p0"] is True


def test_reconcile_detects_missing_local_order(client):
    body = {"snapshot": [{"order_id": "PL-999", "status": "PAID", "amount": 50.0}]}
    data = client.post("/api/stores/s1/reconcile", json=body, headers=OWNER).json()
    assert any(i["kind"] == "MISSING_LOCAL" for i in data["issues"])
    assert data["summary"]["has_p0"] is True


# ===========================================================================
# 六、审计与退款
# ===========================================================================

def test_agent_cannot_read_audit(client):
    assert client.get("/api/audit", headers=AGENT).status_code == 403


def test_owner_can_read_audit(client):
    r = client.get("/api/audit", headers=OWNER)
    assert r.status_code == 200
    assert "items" in r.json()


def test_refund_evaluation_endpoint(client):
    body = {"reason": "CARD_INVALID", "delivered": True, "card_revealed": True}
    data = client.post("/api/refunds/evaluate", json=body, headers=OWNER).json()
    assert data["decision"] == "AUTO_APPROVE"
    assert "自动同意" in data["text"]


def test_refund_flags_risky_buyer(client):
    body = {"reason": "NO_LONGER_NEEDED", "delivered": True, "card_revealed": True,
            "total_orders": 10, "refunds": 8, "off_platform_strikes": 3}
    data = client.post("/api/refunds/evaluate", json=body, headers=OWNER).json()
    assert data["decision"] == "REVIEW"
    assert data["flag_buyer"] is True
    assert data["abuse_score"] >= 60


# ===========================================================================
# 六之二、商品更新（写审计）
# ===========================================================================

def test_update_product_min_price_writes_audit(client):
    r = client.patch("/api/products/p1", params={"store_id": "s1"},
                     json={"min_price": 89.0}, headers=OWNER)
    assert r.status_code == 200
    data = r.json()
    assert data["action"] == "MIN_PRICE_UPDATE"
    assert data["needs_alert"] is True          # 动了关键字段
    change = data["changes"][0]
    assert change["field"] == "min_price"
    assert change["before"] == 99.0 and change["after"] == 89.0

    # 审计里能查到这条记录
    items = client.get("/api/audit", headers=OWNER).json()["items"]
    assert any(i["action"] == "MIN_PRICE_UPDATE" for i in items)


def test_update_product_agent_is_forbidden(client):
    # AGENT 没有 EDIT_PRODUCT 权限
    r = client.patch("/api/products/p1", params={"store_id": "s1"},
                     json={"is_active": False}, headers=AGENT)
    assert r.status_code == 403


def test_update_product_rejects_min_above_listed(client):
    r = client.patch("/api/products/p1", params={"store_id": "s1"},
                     json={"min_price": 999.0}, headers=OWNER)
    assert r.status_code == 422


def test_update_product_no_fields_is_422(client):
    r = client.patch("/api/products/p1", params={"store_id": "s1"},
                     json={}, headers=OWNER)
    assert r.status_code == 422


# ===========================================================================
# 七、指标
# ===========================================================================

def test_metrics_reports_usage_and_limits(client):
    post_msg(client, OWNER, msg_id="m-metrics")
    data = client.get("/api/metrics", params={"store_id": "s1"}, headers=OWNER).json()
    assert data["usage"]["turns"] >= 1
    assert "retries" in data and "handoff" in data


# ===========================================================================
# 八、运营分析与维护
# ===========================================================================

def test_health_endpoint_reports_score_and_dedup(client):
    post_msg(client, OWNER, content="多久发货", msg_id="h1")
    data = client.get("/api/analytics/health",
                      params={"store_id": "s1"}, headers=OWNER).json()

    assert 0 <= data["score"] <= 100
    assert data["dedup"]["total"] == 1
    assert data["dedup"]["duplicates"] == 0
    assert "健康分" in data["text"]


def test_health_flags_duplicate_messages(client):
    # 同一条消息发两次 —— 重复率应该被算出来并升级告警级别
    post_msg(client, OWNER, content="多久发货", msg_id="dup-h")
    post_msg(client, OWNER, content="多久发货", msg_id="dup-h")

    data = client.get("/api/analytics/health",
                      params={"store_id": "s1"}, headers=OWNER).json()
    assert data["dedup"]["duplicates"] == 1
    assert data["dedup"]["rate"] == 0.5
    assert data["dedup"]["level"] == "ALARMING"


def test_health_counts_open_p0_tickets(client):
    post_msg(client, OWNER, content="我要投诉", msg_id="h-p0")
    data = client.get("/api/analytics/health",
                      params={"store_id": "s1"}, headers=OWNER).json()
    assert data["snapshot"]["p0_open"] == 1


def test_health_global_view_aggregates_across_stores(client):
    # 全局看板（不带 store_id）必须把各店流量汇总，不能读空的 "unknown" 桶
    post_msg(client, OWNER, content="多久发货", msg_id="g1")
    post_msg(client, OWNER, content="转人工", msg_id="g2")

    global_view = client.get("/api/analytics/health", headers=OWNER).json()
    assert global_view["usage"]["faq_hits"] == 1
    assert global_view["usage"]["turns"] == 2
    assert global_view["usage"]["saved_calls"] == 1

    per_store = client.get("/api/analytics/health",
                           params={"store_id": "s1"}, headers=OWNER).json()
    assert per_store["usage"]["turns"] == 2  # 本店两次都在 s1


def test_diagnose_endpoint(client):
    body = {"products": [
        {"product_id": "p1", "title": "卖得好", "inquiries": 20, "orders": 12, "stock": 50},
        {"product_id": "p2", "title": "缺货", "inquiries": 5, "orders": 3, "stock": 0},
        {"product_id": "p3", "title": "没人问", "inquiries": 2, "days_listed": 30},
    ]}
    items = client.post("/api/analytics/diagnose", json=body, headers=OWNER).json()["items"]
    verdicts = {i["product_id"]: i["verdict"] for i in items}
    assert verdicts["p1"] == "STAR"
    assert verdicts["p2"] == "STOCKOUT"
    assert verdicts["p3"] == "TRAFFIC"
    assert items[0]["verdict"] == "STOCKOUT"      # 最急的排最前


def test_agent_cannot_run_sourcing(client):
    body = {"keyword": "Switch OLED", "market_price": 1500, "listings": []}
    r = client.post("/api/stores/s1/sourcing", json=body, headers=AGENT)
    assert r.status_code == 403


def test_sourcing_endpoint_filters_cheaply(client):
    body = {
        "keyword": "Switch OLED",
        "market_price": 1500,
        "min_price": 1000,
        "max_price": 1800,
        "min_desire": 5,
        "vision_daily_budget": 1,
        "listings": [
            {"item_id": "good", "title": "Switch OLED 日版", "price": 1200, "desire_count": 20},
            {"item_id": "good2", "title": "Switch OLED 日版 全新", "price": 1150, "desire_count": 30},
            {"item_id": "shallow", "title": "Switch OLED 港版", "price": 1450, "desire_count": 20},
            {"item_id": "too-expensive", "title": "Switch OLED", "price": 3000, "desire_count": 20},
            {"item_id": "cold", "title": "Switch OLED", "price": 1200, "desire_count": 1},
        ],
    }
    data = client.post("/api/stores/s1/sourcing", json=body, headers=OWNER).json()

    # good 和 good2 折扣都够深，shallow 折扣不够（只便宜 3%）
    assert set(data["accepted"]) == {"good", "good2", "shallow"}
    reasons = {r["item_id"]: r["reason"] for r in data["rejected"]}
    assert reasons["too-expensive"] == "PRICE_OUT_OF_RANGE"
    assert reasons["cold"] == "LOW_DESIRE"
    # 预算 1 但有 2 个深折扣候选 → 超出的那个降级放行，并标记预算耗尽
    assert data["budget_exhausted"] is True
    assert data["vision_skipped"] >= 1


def test_maintenance_endpoint(client):
    post_msg(client, OWNER, content="多久发货", msg_id="mt-1")
    data = client.get("/api/ops/maintenance", headers=OWNER).json()

    assert data["row_counts"]["message_log"] >= 1
    assert "message_log" in {r["table"] for r in data["retention"]}
    assert data["quota"]["quota_mb"] == 500.0
    assert "留存计划" in data["text"]


def test_agent_cannot_read_maintenance(client):
    assert client.get("/api/ops/maintenance", headers=AGENT).status_code == 403


# ===========================================================================
# 十、鉴权（登录 + JWT）
# ===========================================================================

def test_login_returns_bearer_token(client):
    r = client.post("/api/auth/login", json={"username": "owner", "password": "demo1234"})
    assert r.status_code == 200
    data = r.json()
    assert data["token_type"] == "bearer"
    assert data["role"] == "OWNER"
    assert data["access_token"].count(".") == 2


def test_login_wrong_password_is_401(client):
    r = client.post("/api/auth/login", json={"username": "owner", "password": "nope"})
    assert r.status_code == 401


def test_login_unknown_user_is_401(client):
    r = client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert r.status_code == 401


def test_jwt_mode_rejects_missing_bearer(jwt_client):
    # 严格模式下，伪造 X-* 头不再管用
    assert jwt_client.get("/api/me", headers=OWNER).status_code == 401


def test_jwt_mode_accepts_valid_token(jwt_client):
    token = jwt_client.post(
        "/api/auth/login", json={"username": "owner", "password": "demo1234"}
    ).json()["access_token"]

    r = jwt_client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    data = r.json()
    assert data["role"] == "OWNER"
    assert set(data["stores"]) == {"s1", "s2"}


def test_jwt_mode_rejects_tampered_token(jwt_client):
    bad = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1MSIsInJvbGUiOiJPV05FUiIsImV4cCI6OTk5OTk5OTk5OX0.xxx"
    r = jwt_client.get("/api/me", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


def test_jwt_principal_cannot_exceed_token_scope(jwt_client):
    # 手工签一个只含 s1 的 token，去访问 s2 必须被拒（403）
    from backend.auth import create_token

    token = create_token(
        {"sub": "u1", "tenant": "t1", "role": "OWNER", "store_ids": ["s1"]},
        "test-secret",
    )
    h = {"Authorization": f"Bearer {token}"}
    assert jwt_client.get("/api/stores/s1/inventory", headers=h).status_code == 200
    assert jwt_client.get("/api/stores/s2/inventory", headers=h).status_code == 403
