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
  msg = adapter.parse_message(await request.body(), request.headers)
  # msg 是平台侧的原始字段（XianyuMessage）。store_id 不在报文里 ——
  # 它由 URL 路由/调用方决定，所以拼装 MessageIn 是调用方的事。
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Optional, Protocol

from pydantic import BaseModel, Field, ValidationError

# 注意：MessageIn 定义在 api.py，而 api.py 又会 import 本模块（注册平台入口），
# 顶层互相 import 会形成循环。这里只在函数内延迟导入，运行时 api 早已加载完毕。


class PlatformAdapter(Protocol):
    """平台适配器协议。"""

    def parse_message(self, body: bytes, headers: dict[str, str]) -> "XianyuMessage":
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
    """闲鱼消息推送的字段子集。实际字段名以官方文档为准。

    长度上限与 `api.py` 的入参模型保持一致 —— 这是**原始报文**进入系统的第一站，
    也是最该卡住的地方：平台推什么长度我们控制不了，而 SQLite 不校验 VARCHAR、
    PostgreSQL 严格拒绝，超长值在本地测试里察觉不到，只会在生产上炸成 500。
    """

    buyer_id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=2000)
    msg_id: str = Field(min_length=1, max_length=128)
    item_id: Optional[str] = Field(default=None, max_length=64)
    order_id: Optional[str] = Field(default=None, max_length=64)
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

    def parse_message(self, body: bytes, headers: dict[str, str]) -> "XianyuMessage":
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
        try:
            msg = XianyuMessage(
                buyer_id=_extract(data, "buyer_id", "buyer_open_id", "user_id"),
                content=_extract(data, "content", "text", "message"),
                msg_id=_extract(data, "msg_id", "message_id", "id"),
                item_id=_extract(data, "item_id", "goods_id", "product_id", default=None),
                order_id=_extract(data, "order_id", "trade_id", default=None),
            )
        except ValidationError as exc:
            # 平台推了超长/空字段。这里是外部输入的边界，必须转成 422 ——
            # 让它冒出去会变成 500，平台看到 5xx 会按重试策略反复重推同一条，
            # 而每次重推都会再失败一次，形成刷屏式报错。
            raise AdapterError(f"推送字段不合法：{_summarize_validation_error(exc)}") from exc

        return msg


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


def _summarize_validation_error(exc: ValidationError) -> str:
    """只取字段名和原因，不把超长原值回显。

    为什么不直接 `str(exc)`：pydantic 会把出错的原值整段塞进消息里。而我们正在
    处理的恰好是「字段过长」这种情况 —— 拿 5000 字的买家 ID 填日志和错误响应，
    只会把小问题放大成大问题。这里只保留长度上限，够定位就行。
    """
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ())) or "?"
        msg = err.get("msg", "不合法")
        limit = err.get("ctx", {}).get("max_length")
        parts.append(f"{loc}（上限 {limit}）" if limit else f"{loc}：{msg}")
    return "；".join(parts)


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
