"""
审计日志测试

两条硬规则的验证：只记真正变化的字段；敏感值必须打码。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.audit import (
    REDACTED,
    Action,
    build_entry,
    diff_changes,
    is_critical,
    is_privileged,
    is_sensitive,
    persist,
    query,
    render,
    summarize,
    trail_for,
)
from backend.guardrails import TenantCtx
from backend.models import AuditLog, Base

run = asyncio.run
NOW = datetime(2026, 9, 13, 19, 0, tzinfo=timezone.utc)
CTX = TenantCtx(tenant_id="t1", user_id="u1")


# ===========================================================================
# 一、字段差异
# ===========================================================================

def test_only_changed_fields_are_recorded():
    changes = diff_changes({"min_price": 99.0, "title": "年卡"}, {"min_price": 89.0, "title": "年卡"})
    assert len(changes) == 1
    assert changes[0].field == "min_price"
    assert (changes[0].before, changes[0].after) == (99.0, 89.0)


def test_int_and_float_equivalents_are_not_a_change():
    assert diff_changes({"min_price": 99}, {"min_price": 99.0}) == ()


def test_identical_snapshots_produce_nothing():
    assert diff_changes({"a": 1, "b": "x"}, {"a": 1, "b": "x"}) == ()


def test_added_and_removed_fields_are_recorded():
    changes = diff_changes({"a": 1}, {"a": 1, "b": 2})
    assert [c.field for c in changes] == ["b"]
    assert changes[0].before is None and changes[0].after == 2


def test_sensitive_values_are_redacted():
    changes = diff_changes(
        {"cookie": "abc123", "vault_ref": "v-1"},
        {"cookie": "def456", "vault_ref": "v-2"},
    )
    assert all(c.before == REDACTED and c.after == REDACTED for c in changes)


def test_sensitive_matching_is_substring_based():
    assert is_sensitive("cookie_string")
    assert is_sensitive("api_key")
    assert is_sensitive("card_content")
    assert not is_sensitive("min_price")


def test_watch_limits_which_fields_are_compared():
    before = {"min_price": 99.0, "updated_at": "t1", "title": "年卡"}
    after = {"min_price": 89.0, "updated_at": "t2", "title": "年卡"}
    changes = diff_changes(before, after, watch=["min_price", "title"])
    assert [c.field for c in changes] == ["min_price"]


def test_critical_fields_are_flagged():
    changes = diff_changes({"min_price": 99.0}, {"min_price": 89.0})
    assert changes[0].critical is True
    assert is_critical("min_price") and is_critical("is_active")
    assert not is_critical("title")


def test_datetime_values_are_json_safe():
    changes = diff_changes({"paid_at": None}, {"paid_at": NOW})
    assert changes[0].after == NOW.isoformat()


def test_as_dict_shape():
    d = diff_changes({"min_price": 99.0}, {"min_price": 89.0})[0].as_dict()
    assert d == {"field": "min_price", "before": 99.0, "after": 89.0, "critical": True}


# ===========================================================================
# 二、审计条目
# ===========================================================================

def test_build_entry_from_snapshots():
    entry = build_entry(
        action=Action.MIN_PRICE_UPDATE, ctx=CTX, actor_role="OWNER",
        target_type="product", target_id="p1", store_id="s1",
        before={"min_price": 99.0}, after={"min_price": 89.0},
    )
    assert entry.fields == ("min_price",)
    assert entry.has_critical_change


def test_build_entry_without_snapshots_keeps_note():
    entry = build_entry(
        action=Action.CARD_EXPORT, ctx=CTX, actor_role="MANAGER",
        target_type="card_pool", target_id="p1", note="导出 200 条",
    )
    assert entry.changes == ()
    assert entry.note == "导出 200 条"


def test_privileged_action_needs_alert():
    assert is_privileged(Action.CARD_EXPORT)
    assert is_privileged(Action.USER_ROLE_CHANGE)
    assert not is_privileged(Action.LOGIN)

    entry = build_entry(action=Action.CARD_EXPORT, ctx=CTX, actor_role="MANAGER",
                        target_type="card_pool", target_id="p1")
    assert entry.needs_alert


def test_critical_field_change_needs_alert_even_if_action_is_ordinary():
    entry = build_entry(
        action=Action.PRODUCT_UPDATE, ctx=CTX, actor_role="OWNER",
        target_type="product", target_id="p1",
        before={"min_price": 99.0}, after={"min_price": 89.0},
    )
    assert entry.needs_alert


def test_ordinary_change_does_not_need_alert():
    entry = build_entry(
        action=Action.PRODUCT_UPDATE, ctx=CTX, actor_role="AGENT",
        target_type="product", target_id="p1",
        before={"title": "旧标题"}, after={"title": "新标题"},
    )
    assert not entry.needs_alert


def test_summarize_and_render():
    entry = build_entry(
        action=Action.MIN_PRICE_UPDATE, ctx=CTX, actor_role="OWNER",
        target_type="product", target_id="p1",
        before={"min_price": 99.0}, after={"min_price": 89.0},
    )
    assert "!min_price" in summarize(entry.changes)
    text = render(entry)
    assert "u1(OWNER)" in text and "product#p1" in text and "99.0 → 89.0" in text


def test_summarize_of_no_change():
    assert summarize(()) == "无字段变更"


def test_render_falls_back_to_note():
    entry = build_entry(action=Action.LOGIN, ctx=CTX, actor_role="OWNER",
                        target_type="user", target_id="u1", note="密码登录")
    assert "密码登录" in render(entry)


# ===========================================================================
# 三、落库与查询
# ===========================================================================

async def _fresh_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _entry(action=Action.MIN_PRICE_UPDATE, target="p1", store="s1",
           before=None, after=None, role="OWNER"):
    return build_entry(
        action=action, ctx=CTX, actor_role=role, target_type="product",
        target_id=target, store_id=store,
        before=before if before is not None else {"min_price": 99.0},
        after=after if after is not None else {"min_price": 89.0},
    )


def test_persist_and_query():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            await persist(db, _entry())
            await persist(db, _entry(target="p2"))
            await db.commit()
            rows = await query(db, CTX)
        await engine.dispose()
        return rows

    rows = run(scenario())
    assert len(rows) == 2
    assert rows[0].changes[0]["field"] == "min_price"


def test_query_filters():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            await persist(db, _entry(target="p1", store="s1"))
            await persist(db, _entry(target="p2", store="s2"))
            # 登录事件不挂店铺，所以按 store_id 过滤时不该出现
            await persist(db, _entry(action=Action.LOGIN, target="u1", store=None))
            await db.commit()
            by_store = await query(db, CTX, store_id="s1")
            by_action = await query(db, CTX, action=Action.LOGIN)
            by_target = await query(db, CTX, target_id="p2")
        await engine.dispose()
        return by_store, by_action, by_target

    by_store, by_action, by_target = run(scenario())
    assert len(by_store) == 1
    assert len(by_action) == 1
    assert len(by_target) == 1


def test_query_only_critical():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            await persist(db, _entry())
            await persist(db, _entry(target="p9",
                                     before={"title": "旧"}, after={"title": "新"}))
            await db.commit()
            rows = await query(db, CTX, only_critical=True)
        await engine.dispose()
        return rows

    rows = run(scenario())
    assert len(rows) == 1
    assert rows[0].target_id == "p1"


def test_trail_for_one_target():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            await persist(db, _entry(target="p1", before={"min_price": 99.0},
                                     after={"min_price": 89.0}))
            await persist(db, _entry(target="p1", before={"min_price": 89.0},
                                     after={"min_price": 85.0}))
            await persist(db, _entry(target="p2"))
            await db.commit()
            rows = await trail_for(db, CTX, "product", "p1")
        await engine.dispose()
        return rows

    rows = run(scenario())
    assert len(rows) == 2
    assert all(r.target_id == "p1" for r in rows)


def test_audit_is_scoped_to_tenant():
    async def scenario():
        engine, Session = await _fresh_db()
        async with Session() as db:
            await persist(db, _entry())
            await db.commit()
            other = TenantCtx(tenant_id="t2", user_id="u9")
            rows = await query(db, other)
        await engine.dispose()
        return rows

    assert run(scenario()) == []
