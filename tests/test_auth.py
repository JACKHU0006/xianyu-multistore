"""
鉴权单元测试

覆盖：密码哈希往返、JWT 签发/校验、防篡改、过期、算法降级攻击、Bearer 解析。
这些都是"一旦写错就是严重安全漏洞"的地方，值得逐条钉死。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from backend.auth import (
    AuthError,
    InvalidToken,
    TokenExpired,
    bearer_token,
    create_token,
    decode_token,
    hash_password,
    verify_password,
)

SECRET = "unit-test-secret"


# ===========================================================================
# 一、密码
# ===========================================================================

def test_password_roundtrip():
    encoded = hash_password("demo1234")
    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password("demo1234", encoded)


def test_wrong_password_rejected():
    encoded = hash_password("demo1234")
    assert not verify_password("wrong", encoded)


def test_same_password_different_salt():
    # 同密码两次哈希必须不同（每用户独立盐），否则相等哈希会泄露"两人同密码"
    assert hash_password("same") != hash_password("same")


def test_malformed_hash_is_rejected_not_crashing():
    assert not verify_password("x", "not-a-valid-hash")
    assert not verify_password("x", "")


def test_empty_password_cannot_be_hashed():
    with pytest.raises(AuthError):
        hash_password("")


# ===========================================================================
# 二、JWT
# ===========================================================================

def test_token_roundtrip():
    token = create_token({"sub": "u1", "tenant": "t1", "role": "OWNER"}, SECRET, now=1000)
    claims = decode_token(token, SECRET, now=1001)
    assert claims["sub"] == "u1"
    assert claims["role"] == "OWNER"
    assert claims["exp"] == 1000 + 12 * 3600


def test_tampered_payload_is_rejected():
    token = create_token({"sub": "u1", "role": "VIEWER"}, SECRET, now=1000)
    h, p, sig = token.split(".")
    forged_payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "u1", "role": "OWNER", "exp": 9999999999}).encode()
    ).decode().rstrip("=")
    forged = f"{h}.{forged_payload}.{sig}"
    with pytest.raises(InvalidToken):
        decode_token(forged, SECRET, now=1001)


def test_wrong_secret_is_rejected():
    token = create_token({"sub": "u1"}, SECRET, now=1000)
    with pytest.raises(InvalidToken):
        decode_token(token, "other-secret", now=1001)


def test_expired_token_is_rejected():
    token = create_token({"sub": "u1"}, SECRET, ttl_seconds=10, now=1000)
    with pytest.raises(TokenExpired):
        decode_token(token, SECRET, now=1011)


def test_alg_none_downgrade_is_rejected():
    # 经典攻击：把 alg 改成 none 并去掉签名。必须被拒。
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "u1", "exp": 9999999999}).encode()
    ).decode().rstrip("=")
    forged = f"{header}.{payload}."
    with pytest.raises(InvalidToken):
        decode_token(forged, SECRET, now=1001)


def test_create_token_without_secret_raises():
    with pytest.raises(AuthError):
        create_token({"sub": "u1"}, "")


# ===========================================================================
# 三、Bearer 解析
# ===========================================================================

def test_bearer_parsing():
    assert bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"
    assert bearer_token("bearer abc") == "abc"
    assert bearer_token("Token abc") is None
    assert bearer_token(None) is None
    assert bearer_token("") is None
