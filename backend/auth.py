"""
鉴权：密码哈希 + JWT 签发/校验
==============================

为什么不用 PyJWT / passlib
--------------------------
只做 HS256 一种算法，标准库的 `hmac` + `hashlib` 就能安全实现，少一个依赖就少一个
供应链攻击面。密码用 PBKDF2-HMAC-SHA256（标准库），迭代次数拉高、每用户独立随机盐。

两条铁律
--------
1. 密码**只存哈希**，永不落地明文；校验用 `hmac.compare_digest`（常量时间，防时序侧信道）。
2. 登录失败**不区分**"用户不存在"和"密码错误" —— 否则等于送了一个用户名枚举接口。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Optional

PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16
JWT_ALG = "HS256"
DEFAULT_TTL_SECONDS = 12 * 3600


class AuthError(Exception):
    pass


class TokenExpired(AuthError):
    pass


class InvalidToken(AuthError):
    pass


# ===========================================================================
# 一、密码
# ===========================================================================

def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    if not password:
        raise AuthError("密码不能为空")
    salt = salt or os.urandom(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64e(salt)}${_b64e(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iters, salt_b64, dk_b64 = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = _b64d(salt_b64)
        expected = _b64d(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:  # noqa: BLE001 - 任何异常都视为校验失败
        return False


# ===========================================================================
# 二、JWT（HS256）
# ===========================================================================

def _sign(signing_input: bytes, secret: str) -> str:
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return _b64e(sig)


def create_token(
    claims: dict[str, Any],
    secret: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: Optional[int] = None,
) -> str:
    if not secret:
        raise AuthError("未配置 JWT_SECRET")
    issued = int(now if now is not None else time.time())
    payload = {**claims, "iat": issued, "exp": issued + ttl_seconds}
    header = {"alg": JWT_ALG, "typ": "JWT"}
    seg = ".".join([
        _b64e(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")),
    ])
    signature = _sign(seg.encode("ascii"), secret)
    return seg + "." + signature


def decode_token(token: str, secret: str, *, now: Optional[int] = None) -> dict[str, Any]:
    if not secret:
        raise AuthError("未配置 JWT_SECRET")
    try:
        h_b64, p_b64, sig = token.split(".")
    except ValueError as exc:
        raise InvalidToken("token 格式不正确") from exc

    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    if not hmac.compare_digest(_sign(signing_input, secret), sig):
        raise InvalidToken("签名不匹配")

    try:
        header = json.loads(_b64d(h_b64))
        payload = json.loads(_b64d(p_b64))
    except Exception as exc:  # noqa: BLE001
        raise InvalidToken("token 载荷无法解析") from exc

    if header.get("alg") != JWT_ALG:
        raise InvalidToken("不支持的签名算法")

    exp = payload.get("exp")
    current = int(now if now is not None else time.time())
    if exp is None or current >= int(exp):
        raise TokenExpired("token 已过期")
    return payload


def bearer_token(authorization: Optional[str]) -> Optional[str]:
    """从 Authorization 头里抠出 Bearer token。"""
    if not authorization:
        return None
    prefix = "bearer "
    if authorization.lower().startswith(prefix):
        return authorization[len(prefix):].strip() or None
    return None
