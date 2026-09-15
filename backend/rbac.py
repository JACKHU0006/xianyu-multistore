"""
角色权限

两个维度，别混在一起
--------------------
很多权限系统做错的地方是把"能做什么"和"能看哪些数据"揉成一个东西。这里分开：

  **能力（Permission）**：由角色决定。客服能不能回复消息？能不能改底价？
  **范围（Scope）**：由 store_ids 决定。这个店主只能看自己那家店。

分开之后，"运营主管看全部店铺但不能改别人角色"这种规则才表达得清楚。

四个角色
--------
  OWNER   店主。自己店铺的全权，包括加人。
  MANAGER 运营主管。租户内全部店铺的经营权限，**但不能改角色** ——
          这是刻意的：能改角色就能给自己提权，等于权限体系失效。
  AGENT   客服。只能看会话和商品，能回复、能申请人工，看不到底价。
  VIEWER  只读。给财务、给老板看报表用。

最容易被忽略的一条：**客服看不到底价**。
底价是这家店最核心的商业机密，客服流动性大，把底价暴露给客服等于把
"你能砍到多少"直接告诉所有离职员工。所以 `redact_product()` 会把底价从
返回数据里摘掉，而不是靠前端不显示。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional


class Role:
    OWNER = "OWNER"
    MANAGER = "MANAGER"
    AGENT = "AGENT"
    VIEWER = "VIEWER"


ALL_ROLES = frozenset({Role.OWNER, Role.MANAGER, Role.AGENT, Role.VIEWER})

ROLE_RANK: dict[str, int] = {
    Role.VIEWER: 0,
    Role.AGENT: 1,
    Role.MANAGER: 2,
    Role.OWNER: 3,
}


class Perm:
    VIEW_DASHBOARD = "dashboard.view"
    VIEW_MESSAGES = "messages.view"
    REPLY_MESSAGE = "messages.reply"
    ASSIGN_HANDOFF = "handoff.assign"

    VIEW_PRODUCT = "product.view"
    EDIT_PRODUCT = "product.edit"
    VIEW_MIN_PRICE = "price.min.view"
    EDIT_MIN_PRICE = "price.min.edit"

    MANAGE_CARDS = "cards.manage"
    REVEAL_CARD = "cards.reveal"

    VIEW_ORDER = "order.view"
    SHIP = "order.ship"          # 手动触发发货/补发

    MANAGE_MONITOR = "monitor.manage"
    MANAGE_STORE = "store.manage"
    MANAGE_USERS = "users.manage"
    VIEW_AUDIT = "audit.view"
    EXPORT_DATA = "data.export"


ALL_PERMISSIONS = frozenset({
    Perm.VIEW_DASHBOARD, Perm.VIEW_MESSAGES, Perm.REPLY_MESSAGE, Perm.ASSIGN_HANDOFF,
    Perm.VIEW_PRODUCT, Perm.EDIT_PRODUCT, Perm.VIEW_MIN_PRICE, Perm.EDIT_MIN_PRICE,
    Perm.MANAGE_CARDS, Perm.REVEAL_CARD, Perm.VIEW_ORDER, Perm.SHIP,
    Perm.MANAGE_MONITOR, Perm.MANAGE_STORE,
    Perm.MANAGE_USERS, Perm.VIEW_AUDIT, Perm.EXPORT_DATA,
})

# 客服：会话 + 商品只读。没有 VIEW_MIN_PRICE，所以看不到底价；
# 没有 SHIP，因为发货是自动化的事，人工介入要走主管。
_AGENT_PERMS = frozenset({
    Perm.VIEW_DASHBOARD, Perm.VIEW_MESSAGES, Perm.REPLY_MESSAGE,
    Perm.ASSIGN_HANDOFF, Perm.VIEW_PRODUCT, Perm.VIEW_ORDER,
})

_VIEWER_PERMS = frozenset({
    Perm.VIEW_DASHBOARD, Perm.VIEW_MESSAGES, Perm.VIEW_PRODUCT, Perm.VIEW_ORDER,
})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.VIEWER: _VIEWER_PERMS,
    Role.AGENT: _AGENT_PERMS,
    # 主管拿全部经营权限，唯独不能改角色 —— 防止自我提权
    Role.MANAGER: ALL_PERMISSIONS - {Perm.MANAGE_USERS},
    Role.OWNER: ALL_PERMISSIONS,
}

# 商品数据里属于"商业机密"的字段，客服不可见
PROTECTED_PRODUCT_FIELDS = ("min_price", "bargain_ladder", "cost_price")


class PermissionDenied(Exception):
    def __init__(self, user_id: str, perm: str, store_id: Optional[str] = None) -> None:
        self.user_id, self.perm, self.store_id = user_id, perm, store_id
        where = f"（店铺 {store_id}）" if store_id else ""
        super().__init__(f"用户 {user_id} 没有权限 {perm}{where}")


@dataclass(frozen=True)
class Principal:
    """
    当前请求的身份。

    store_ids = None 表示"租户内全部店铺"，用于 MANAGER。
    其余角色必须是显式集合 —— 默认给全部是最常见的越权来源，
    所以这里刻意不给默认值。
    """

    user_id: str
    tenant_id: str
    role: str
    store_ids: Optional[frozenset[str]] = None

    @property
    def tenant_wide(self) -> bool:
        return self.store_ids is None


def permissions_of(role: str) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def can(principal: Principal, perm: str) -> bool:
    return perm in permissions_of(principal.role)


def can_access_store(principal: Principal, store_id: str) -> bool:
    if principal.store_ids is None:
        return True
    return store_id in principal.store_ids


def check(principal: Principal, perm: str, *, store_id: Optional[str] = None) -> None:
    """能力 + 范围双重校验。任何一个不过都拒绝。"""
    if not can(principal, perm):
        raise PermissionDenied(principal.user_id, perm, store_id)
    if store_id is not None and not can_access_store(principal, store_id):
        raise PermissionDenied(principal.user_id, perm, store_id)


def visible_stores(principal: Principal, all_store_ids: Iterable[str]) -> frozenset[str]:
    """过滤出这个身份能看到的店铺。列表接口必须走这里，不能直接返回全部。"""
    if principal.store_ids is None:
        return frozenset(all_store_ids)
    return frozenset(s for s in all_store_ids if s in principal.store_ids)


# ---------------------------------------------------------------------------
# 数据脱敏
# ---------------------------------------------------------------------------

def redact_product(principal: Principal, data: Mapping[str, Any]) -> dict:
    """
    按权限摘掉不该看到的字段。

    在**服务端返回前**做，而不是靠前端不渲染 —— 前端不渲染的字段，
    打开 F12 就全看见了。
    """
    out = dict(data)
    if can(principal, Perm.VIEW_MIN_PRICE):
        return out
    for field_name in PROTECTED_PRODUCT_FIELDS:
        out.pop(field_name, None)
    out["min_price_hidden"] = True
    return out


def redact_products(principal: Principal, items: Iterable[Mapping[str, Any]]) -> list[dict]:
    return [redact_product(principal, item) for item in items]


# ---------------------------------------------------------------------------
# 授权（防止提权）
# ---------------------------------------------------------------------------

def can_grant_role(principal: Principal, target_role: str) -> bool:
    """
    能不能把某个人设成 target_role。

    两条约束：
      1. 必须持有 MANAGE_USERS
      2. **不能授予高于自己的角色** —— 否则一个 MANAGER 可以把自己提成 OWNER
    """
    if target_role not in ALL_ROLES:
        return False
    if not can(principal, Perm.MANAGE_USERS):
        return False
    return ROLE_RANK[target_role] <= ROLE_RANK[principal.role]


def assert_can_grant_role(principal: Principal, target_role: str) -> None:
    if not can_grant_role(principal, target_role):
        raise PermissionDenied(principal.user_id, f"grant_role:{target_role}")


def can_modify_user(principal: Principal, target_user_id: str, target_role: str) -> bool:
    """
    能不能改某个用户。

    不能改自己 —— 自我提权最常见的路径就是"先改自己"。
    """
    if principal.user_id == target_user_id:
        return False
    return can_grant_role(principal, target_role)


def describe(principal: Principal) -> str:
    scope = "全部店铺" if principal.tenant_wide else f"{len(principal.store_ids or ())} 家店铺"
    return f"{principal.user_id} / {principal.role} / {scope}"
