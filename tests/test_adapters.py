"""
平台 webhook 适配器测试

验证"平台原始格式 → 内部 MessageIn"的转换，以及签名校验与事件过滤。
这些是接入真实平台时最先踩坑的地方：字段名对不上、签名算法不一致、
把订单事件当消息处理。
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from backend.adapters import (
    AdapterError,
    SignatureMismatch,
    UnsupportedEvent,
    XianyuAdapter,
    get_adapter,
)


def _body(**data) -> bytes:
    return json.dumps({"event_type": "message", "data": data}, ensure_ascii=False).encode("utf-8")


# ===========================================================================
# 一、字段映射
# ===========================================================================

def test_parses_canonical_fields():
    adapter = XianyuAdapter()
    msg = adapter.parse_message(
        _body(buyer_id="b1", content="在吗", msg_id="m1", item_id="ITEM-1"), {}
    )
    assert msg.buyer_id == "b1"
    assert msg.content == "在吗"
    assert msg.msg_id == "m1"
    assert msg.item_id == "ITEM-1"


def test_parses_alternate_field_names():
    # 真实平台字段名经常和文档不一致，适配器要能容错
    adapter = XianyuAdapter()
    body = json.dumps({
        "type": "buyer_message",
        "data": {"buyer_open_id": "b9", "text": "便宜点", "message_id": "m9", "goods_id": "G-9"},
    }, ensure_ascii=False).encode("utf-8")
    msg = adapter.parse_message(body, {})
    assert msg.buyer_id == "b9"
    assert msg.content == "便宜点"
    assert msg.msg_id == "m9"
    assert msg.item_id == "G-9"


def test_missing_required_field_raises():
    adapter = XianyuAdapter()
    body = json.dumps({"event_type": "message", "data": {"content": "在吗"}}, ensure_ascii=False).encode()
    with pytest.raises(AdapterError):
        adapter.parse_message(body, {})


def test_non_json_body_raises():
    adapter = XianyuAdapter()
    with pytest.raises(AdapterError):
        adapter.parse_message(b"not-json", {})


# ===========================================================================
# 二、签名校验
# ===========================================================================

def test_signature_ok_when_secret_matches():
    secret = "s3cr3t"
    body = _body(buyer_id="b1", content="在吗", msg_id="m1")
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    adapter = XianyuAdapter(sign_secret=secret)
    msg = adapter.parse_message(body, {"X-Signature": sig})
    assert msg.buyer_id == "b1"


def test_signature_mismatch_is_rejected():
    body = _body(buyer_id="b1", content="在吗", msg_id="m1")
    adapter = XianyuAdapter(sign_secret="s3cr3t")
    with pytest.raises(SignatureMismatch):
        adapter.parse_message(body, {"X-Signature": "deadbeef"})


def test_no_secret_configured_passes_through():
    # 开发期未配密钥应放行，而不是把消息全挡掉
    adapter = XianyuAdapter(sign_secret=None)
    msg = adapter.parse_message(_body(buyer_id="b1", content="x", msg_id="m1"), {})
    assert msg.buyer_id == "b1"


# ===========================================================================
# 三、事件过滤
# ===========================================================================

def test_order_event_is_not_treated_as_message():
    body = json.dumps({"event_type": "order_paid", "data": {"order_id": "PL-1"}}).encode()
    adapter = XianyuAdapter()
    with pytest.raises(UnsupportedEvent):
        adapter.parse_message(body, {})


# ===========================================================================
# 四、注册表
# ===========================================================================

def test_registry_returns_xianyu_adapter():
    adapter = get_adapter("xianyu", sign_secret="k")
    assert isinstance(adapter, XianyuAdapter)


def test_registry_unknown_platform_raises():
    with pytest.raises(AdapterError):
        get_adapter("taobao")
