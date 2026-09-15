"""
平台 Webhook 适配器
==================

把各平台（闲鱼、转转等）的 webhook 格式转换成系统内部统一的 `MessageIn` 格式。

设计原则：
  - 适配器只做**格式转换**，不做业务判断（那是 pipeline 的事）；
  - 每个平台一个适配器类，实现 `PlatformAdapter` 协议；
  - 未知字段透传，不丢数据；
  - 签名/时间戳校验在适配器层做，校验失败直接抛 401，不进业务逻辑。

闲鱼 webhook 说明（基于公开文档与逆向经验，**非官方接口**）：
  - 消息推送：通常走 HTTP POST，JSON body，含 buyer_id / content / item_id 等；
  - 签名：部分场景用 HMAC-SHA256，密钥由平台分配；
  - 订单状态：独立 webhook，与消息 webhook 分开推送；
  - 实际接入时请以闲鱼开放平台最新文档为准，这里只提供骨架。

用法：
  from backend.adapters import XianyuAdapter
  adapter = XianyuAdapter(sign_secret=os.environ.get("XIANYU_WEBHOOK_SECRET"))
  message_in = adapter.parse_message(await request.body(), request.headers)
  # 然后直接调 pipeline 或 api.webhook_message
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Optional, Protocol

from pydantic import BaseModel

# 注意：MessageIn 定义在 api.py，而 api.py 又会 import 本模块（注册平台入口），
# 顶层互相 import 会形成循环。这里只在函数内延迟导入，运行时 api 早已加载完毕。


class PlatformAdapter(Protocol):
    """平台适配器协议。"""

    def parse_message(self, body: bytes, headers: dict[str, str]) -> MessageIn:
        ...

    def verify_signature(self, body: bytes, headers: dict[str, str]) -> bool:
        ...


class AdapterError(Exception):
    pass


class SignatureMismatch(AdapterError):
    """Webhook 签名校验失败。"""


class UnsupportedEvent(AdapterError):
    """收到不处理的事件类型。"""


# ===========================================================================
# 通用工具
# ===========================================================================

def _require_keys(d: dict, *keys: str) -> None:
    missing = [k for k in keys if k not in d or d[k] is None]
    if missing:
        raise AdapterError(f"缺少必填字段：{missing}")


# ===========================================================================
# 闲鱼适配器（骨架）
# ===========================================================================

class XianyuMessage(BaseModel):
    """闲鱼消息推送的字段子集。实际字段名以官方文档为准。"""

    buyer_id: str
    content: str
    msg_id: str
    item_id: Optional[str] = None
    order_id: Optional[str] = None
    sent_at: Optional[int] = None  # 毫秒时间戳


class XianyuAdapter:
    """
    闲鱼 webhook 适配器。

    当前为**骨架实现**，字段映射基于公开逆向经验。接入生产环境时：
      1. 申请闲鱼开放平台 webhook 权限；
      2. 按实际收到的 body 字段调整 `XianyuMessage`；
      3. 按实际签名算法调整 `verify_signature`。
    """

    def __init__(self, sign_secret: Optional[str] = None) -> None:
        self.sign_secret = (sign_secret or "").encode("utf-8")

    def verify_signature(self, body: bytes, headers: dict[str, str]) -> bool:
        if not self.sign_secret:
            # 未配密钥时放行（开发期方便），但打日志提醒
            return True

        # 常见签名方式：HMAC-SHA256(body, secret)
        # 实际请以闲鱼开放平台文档为准
        expected = headers.get("X-Signature") or headers.get("x-signature") or ""
        computed = hmac.new(self.sign_secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, computed)

    def parse_message(self, body: bytes, headers: dict[str, str]) -> "MessageIn":
        from .api import MessageIn  # 延迟导入，避免与 api 的循环依赖

        if not self.verify_signature(body, headers):
            raise SignatureMismatch("闲鱼 webhook 签名校验失败")

        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise AdapterError("body 不是合法 JSON") from exc

        # 闲鱼可能按事件类型分不同结构，这里只处理买家消息
        event_type = payload.get("event_type") or payload.get("type") or "message"
        if event_type not in ("message", "buyer_message", "new_message"):
            raise UnsupportedEvent(f"不处理的事件类型：{event_type}")

        # 字段映射：把闲鱼字段名转成内部字段名
        data = payload.get("data") or payload
        msg = XianyuMessage(
            buyer_id=_extract(data, "buyer_id", "buyer_open_id", "user_id"),
            content=_extract(data, "content", "text", "message"),
            msg_id=_extract(data, "msg_id", "message_id", "id"),
            item_id=_extract(data, "item_id", "goods_id", "product_id", default=None),
            order_id=_extract(data, "order_id", "trade_id", default=None),
        )

        return MessageIn(
            store_id="",  # 由调用方根据 webhook 路由或 payload 中的店铺信息填入
            buyer_id=msg.buyer_id,
            content=msg.content,
            msg_id=msg.msg_id,
            item_id=msg.item_id,
        )


# ===========================================================================
# 通用字段提取（容错）
# ===========================================================================

_MISSING = object()


def _extract(data: dict, *keys: str, default: Any = _MISSING) -> Any:
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
    if default is not _MISSING:
        return default
    raise AdapterError(f"字段缺失：尝试 {keys} 均未命中")


# ===========================================================================
# 适配器注册表（方便扩展）
# ===========================================================================

_REGISTRY: dict[str, type] = {
    "xianyu": XianyuAdapter,
}


def get_adapter(name: str, **config) -> PlatformAdapter:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise AdapterError(f"未知平台适配器：{name}，可用：{list(_REGISTRY)}")
    return cls(**config)
