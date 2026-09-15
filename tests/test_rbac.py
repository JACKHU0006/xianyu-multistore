"""
角色权限测试

重点验证两件容易做错的事：
  1. 能力与数据范围分开 —— 主管有权限，但客服只看自己那家店
  2. 不能提权 —— 谁都不能把自己或别人提到比自己高
"""

from __future__ import annotations

import pytest

from backend.rbac import (
    ALL_PERMISSIONS,
    ALL_ROLES,
    Perm,
    PermissionDenied,
    Principal,
    ROLE_PERMISSIONS,
    Role,
    can,
    can_access_store,
    can_grant_role,
    can_modify_user,
    check,
    describe,
    permissions_of,
    redact_product,
    redact_products,
    visible_stores,
)

OWNER = Principal(user_id="u1", tenant_id="t1", role=Role.OWNER, store_ids=frozenset({"s1"}))
MANAGER = Principal(user_id="u2", tenant_id="t1", role=Role.MANAGER, store_ids=None)
AGENT = Principal(user_id="u3", tenant_id="t1", role=Role.AGENT, store_ids=frozenset({"s1"}))
VIEWER = Principal(user_id="u4", tenant_id="t1", role=Role.VIEWER, store_ids=frozenset({"s1"}))


# ===========================================================================
# 一、能力
# ===========================================================================

def test_owner_has_every_permission():
    assert permissions_of(Role.OWNER) == ALL_PERMISSIONS


def test_manager_has_everything_except_user_management():
    assert can(MANAGER, Perm.MANAGE_STORE)
    assert can(MANAGER, Perm.EDIT_MIN_PRICE)
    assert not can(MANAGER, Perm.MANAGE_USERS)


def test_agent_cannot_see_or_edit_the_floor_price():
    assert can(AGENT, Perm.REPLY_MESSAGE)
    assert can(AGENT, Perm.VIEW_PRODUCT)
    assert not can(AGENT, Perm.VIEW_MIN_PRICE)
    assert not can(AGENT, Perm.EDIT_MIN_PRICE)
    assert not can(AGENT, Perm.EDIT_PRODUCT)


def test_agent_cannot_touch_cards_or_monitors():
    assert not can(AGENT, Perm.MANAGE_CARDS)
    assert not can(AGENT, Perm.REVEAL_CARD)
    assert not can(AGENT, Perm.MANAGE_MONITOR)


def test_viewer_is_read_only():
    assert can(VIEWER, Perm.VIEW_DASHBOARD)
    assert can(VIEWER, Perm.VIEW_MESSAGES)
    assert not can(VIEWER, Perm.REPLY_MESSAGE)
    assert not can(VIEWER, Perm.ASSIGN_HANDOFF)
    assert not can(VIEWER, Perm.EXPORT_DATA)


def test_unknown_role_has_no_permissions():
    ghost = Principal(user_id="x", tenant_id="t1", role="GHOST", store_ids=frozenset())
    assert permissions_of("GHOST") == frozenset()
    assert not can(ghost, Perm.VIEW_DASHBOARD)


def test_all_roles_are_mapped():
    for role in ALL_ROLES:
        assert role in ROLE_PERMISSIONS
        assert permissions_of(role)          # 每个角色都至少有一个权限


# ===========================================================================
# 二、数据范围
# ===========================================================================

def test_tenant_wide_principal_reaches_every_store():
    assert can_access_store(MANAGER, "s1")
    assert can_access_store(MANAGER, "s99")


def test_scoped_principal_only_reaches_its_stores():
    assert can_access_store(OWNER, "s1")
    assert not can_access_store(OWNER, "s2")


def test_visible_stores_filters():
    all_stores = ["s1", "s2", "s3"]
    assert visible_stores(OWNER, all_stores) == frozenset({"s1"})
    assert visible_stores(MANAGER, all_stores) == frozenset(all_stores)


def test_visible_stores_ignores_unknown_ids():
    assert visible_stores(OWNER, ["s2", "s3"]) == frozenset()


# ===========================================================================
# 三、双重校验
# ===========================================================================

def test_check_passes_when_capability_and_scope_align():
    check(OWNER, Perm.EDIT_MIN_PRICE, store_id="s1")


def test_check_rejects_missing_capability():
    with pytest.raises(PermissionDenied):
        check(AGENT, Perm.EDIT_MIN_PRICE, store_id="s1")


def test_check_rejects_out_of_scope_even_with_capability():
    # 有能力但越范围 —— 这是最容易漏掉的一种越权
    with pytest.raises(PermissionDenied):
        check(OWNER, Perm.EDIT_MIN_PRICE, store_id="s2")


def test_permission_denied_carries_context():
    with pytest.raises(PermissionDenied) as exc:
        check(AGENT, Perm.MANAGE_CARDS, store_id="s1")
    assert exc.value.perm == Perm.MANAGE_CARDS
    assert "s1" in str(exc.value)


# ===========================================================================
# 四、数据脱敏
# ===========================================================================

PRODUCT = {
    "id": "p1", "title": "爱奇艺黄金会员 年卡",
    "listed_price": 128.0, "min_price": 99.0,
    "bargain_ladder": [0.05, 0.12], "cost_price": 70.0,
}


def test_agent_cannot_see_floor_price_in_payload():
    out = redact_product(AGENT, PRODUCT)
    assert "min_price" not in out
    assert "bargain_ladder" not in out
    assert "cost_price" not in out
    assert out["min_price_hidden"] is True
    assert out["listed_price"] == 128.0      # 挂牌价是公开的，保留


def test_owner_and_manager_see_everything():
    assert redact_product(OWNER, PRODUCT) == PRODUCT
    assert redact_product(MANAGER, PRODUCT) == PRODUCT


def test_redaction_does_not_mutate_the_source():
    redact_product(AGENT, PRODUCT)
    assert "min_price" in PRODUCT


def test_redact_products_maps_over_a_list():
    out = redact_products(AGENT, [PRODUCT, PRODUCT])
    assert len(out) == 2
    assert all("min_price" not in item for item in out)


# ===========================================================================
# 五、防止提权
# ===========================================================================

def test_owner_can_grant_any_role():
    for role in ALL_ROLES:
        assert can_grant_role(OWNER, role)


def test_manager_cannot_grant_anything():
    # 没有 MANAGE_USERS，所以完全不能改角色
    assert not can_grant_role(MANAGER, Role.AGENT)
    assert not can_grant_role(MANAGER, Role.MANAGER)


def test_agent_and_viewer_cannot_grant():
    assert not can_grant_role(AGENT, Role.VIEWER)
    assert not can_grant_role(VIEWER, Role.VIEWER)


def test_cannot_grant_unknown_role():
    assert not can_grant_role(OWNER, "SUPERADMIN")


def test_nobody_can_modify_themselves():
    # 自我提权最常见的路径就是"先改自己"
    assert not can_modify_user(OWNER, "u1", Role.OWNER)
    assert not can_modify_user(MANAGER, "u2", Role.AGENT)


def test_owner_can_modify_other_users():
    assert can_modify_user(OWNER, "u3", Role.AGENT)
    assert can_modify_user(OWNER, "u9", Role.MANAGER)


def test_manager_cannot_modify_other_users():
    assert not can_modify_user(MANAGER, "u3", Role.AGENT)


def test_describe_reports_scope():
    assert "全部店铺" in describe(MANAGER)
    assert "1 家店铺" in describe(OWNER)
