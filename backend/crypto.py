"""
卡密加密与密钥服务

为什么不能"一把密钥加密所有卡密"
--------------------------------
卡密池是这套系统里最值钱的东西 —— 拿到就能直接换成钱。所以威胁模型不是
"外部攻击者"，而是**内部**：一个离职的运维、一个被拖库的备份、一份误传的
数据库快照。

如果全库一把密钥，上面任何一种情况都等于整个卡密池裸奔。所以用**信封加密 +
每店独立密钥**：

    主密钥（KEK，放环境变量 / KMS，不进数据库）
        │  HKDF 派生（info = 店铺 ID）
        ▼
    店铺密钥（DEK）
        │  AES-256-GCM
        ▼
    卡密密文（进数据库）

三个直接收益：
  1. 拖走数据库 ≠ 拿到卡密 —— 没有主密钥解不开
  2. 泄露一家店的密钥不影响其他店
  3. GCM 是认证加密，密文被篡改会直接解密失败，而不是解出垃圾数据

还有一条容易被忽略的：**AAD 绑定店铺 ID**。这意味着 A 店的密文就算被搬到
B 店的行里，解密也会失败 —— 防止"把便宜店的卡密挪到贵店的池子里"这种内鬼操作。

明文绝不落库、绝不进日志。`mask()` 用于展示，`content_fingerprint()` 用于去重。
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Optional, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TOKEN_VERSION = "v1"
NONCE_BYTES = 12          # GCM 推荐 96 bit
KEY_BYTES = 32            # AES-256
_HKDF_SALT = b"xianyu-multistore/v1"


class CryptoError(Exception):
    pass


class KeyMaterialMissing(CryptoError):
    pass


class DecryptionError(CryptoError):
    pass


# ===========================================================================
# 一、密钥派生
# ===========================================================================

def generate_master_key() -> str:
    """生成一个新的主密钥（base64）。放进环境变量 MASTER_KEY，不要提交进仓库。"""
    return base64.b64encode(os.urandom(KEY_BYTES)).decode("ascii")


def load_master_key(raw: str) -> bytes:
    """
    解析主密钥。接受 base64 或 hex 两种写法 —— 运维从不同工具复制出来的格式
    经常不一致，与其让他们踩坑，不如都认。
    """
    if not raw:
        raise KeyMaterialMissing("未配置主密钥")
    raw = raw.strip()
    try:
        key = base64.b64decode(raw, validate=True)
        if len(key) == KEY_BYTES:
            return key
    except Exception:  # noqa: BLE001 - 换 hex 再试
        pass
    try:
        key = bytes.fromhex(raw)
        if len(key) == KEY_BYTES:
            return key
    except ValueError:
        pass
    raise KeyMaterialMissing("主密钥格式不对，需要 32 字节的 base64 或 hex")


def derive_store_key(master: bytes, key_ref: str) -> bytes:
    """用 HKDF 从主密钥派生某个店铺的密钥。同样的输入永远得到同样的输出。"""
    if not key_ref:
        raise KeyMaterialMissing("key_ref（店铺标识）不能为空")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=_HKDF_SALT,
        info=key_ref.encode("utf-8"),
    ).derive(master)


# ===========================================================================
# 二、密文封装
# ===========================================================================

def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


@dataclass(frozen=True)
class SealedToken:
    """
    密文的自描述格式：  v1.<key_ref>.<nonce>.<ciphertext>

    把 key_ref 写进密文里，是为了让"用哪个密钥解"这件事不依赖调用方传对参数 ——
    传错了会立刻失败，而不是静默解出别家店的卡密。
    """

    key_ref: str
    nonce: bytes
    ciphertext: bytes
    version: str = TOKEN_VERSION

    def to_str(self) -> str:
        if "." in self.key_ref:
            raise ValueError("key_ref 不能包含 '.'")
        return ".".join([
            self.version, self.key_ref, _b64e(self.nonce), _b64e(self.ciphertext),
        ])

    @classmethod
    def parse(cls, token: str) -> "SealedToken":
        parts = token.split(".")
        if len(parts) != 4:
            raise DecryptionError("密文格式不正确")
        version, key_ref, nonce, ciphertext = parts
        if version != TOKEN_VERSION:
            raise DecryptionError(f"不支持的密文版本 {version}")
        try:
            return cls(key_ref=key_ref, nonce=_b64d(nonce), ciphertext=_b64d(ciphertext))
        except Exception as exc:  # noqa: BLE001
            raise DecryptionError("密文解码失败") from exc


# ===========================================================================
# 三、密钥服务
# ===========================================================================

class KeyProvider(Protocol):
    def encrypt(self, plaintext: str, key_ref: str) -> str: ...
    def decrypt(self, token: str, key_ref: Optional[str] = None) -> str: ...


class LocalKeyProvider:
    """
    本地实现：主密钥来自环境变量，店铺密钥现场派生。

    生产环境应该换成 KMS / Vault 实现 —— 接口一样，只是 derive 那一步改成
    远程调用。这样主密钥连进程内存都不出现。
    """

    def __init__(self, master_key: bytes | str) -> None:
        self._master = load_master_key(master_key) if isinstance(master_key, str) else master_key
        if len(self._master) != KEY_BYTES:
            raise KeyMaterialMissing("主密钥长度必须是 32 字节")

    def _key(self, key_ref: str) -> bytes:
        return derive_store_key(self._master, key_ref)

    def encrypt(self, plaintext: str, key_ref: str) -> str:
        if plaintext is None:
            raise CryptoError("不能加密 None")
        nonce = os.urandom(NONCE_BYTES)
        # AAD 绑定 key_ref：密文被搬到别的店铺行里就解不开
        aad = key_ref.encode("utf-8")
        ct = AESGCM(self._key(key_ref)).encrypt(nonce, plaintext.encode("utf-8"), aad)
        return SealedToken(key_ref=key_ref, nonce=nonce, ciphertext=ct).to_str()

    def decrypt(self, token: str, key_ref: Optional[str] = None) -> str:
        sealed = SealedToken.parse(token)
        if key_ref is not None and key_ref != sealed.key_ref:
            raise DecryptionError(
                f"密钥不匹配：密文属于 {sealed.key_ref}，却用 {key_ref} 去解"
            )
        try:
            plain = AESGCM(self._key(sealed.key_ref)).decrypt(
                sealed.nonce, sealed.ciphertext, sealed.key_ref.encode("utf-8")
            )
        except InvalidTag as exc:
            # 密文被改过、密钥不对、或者主密钥换过了 —— 都走这里
            raise DecryptionError("解密失败：密文被篡改或密钥不匹配") from exc
        return plain.decode("utf-8")

    def reencrypt(self, token: str, new_key_ref: str) -> str:
        """换店（或换密钥）时重加密：先解开再封上。"""
        return self.encrypt(self.decrypt(token), new_key_ref)


def provider_from_env(var: str = "MASTER_KEY") -> LocalKeyProvider:
    return LocalKeyProvider(load_master_key(os.environ.get(var, "")))


# ---------------------------------------------------------------------------
# 模块级默认 provider（生产代码用；测试请直接构造 LocalKeyProvider）
# ---------------------------------------------------------------------------

_provider: Optional[KeyProvider] = None


def configure_provider(provider: Optional[KeyProvider]) -> None:
    global _provider
    _provider = provider


def get_provider() -> KeyProvider:
    global _provider
    if _provider is None:
        _provider = provider_from_env()
    return _provider


# ===========================================================================
# 四、展示与去重
# ===========================================================================

def mask(secret: str, keep_head: int = 4, keep_tail: int = 4) -> str:
    """
    展示用掩码。保留头尾便于人工核对，中间一律打掉。

    短于头尾之和时全部打掉 —— 否则"保留 4 位"会把 6 位卡密泄露掉 4 位。
    """
    if not secret:
        return ""
    if len(secret) <= keep_head + keep_tail:
        return "*" * len(secret)
    return secret[:keep_head] + "*" * (len(secret) - keep_head - keep_tail) + secret[-keep_tail:]


def content_fingerprint(plaintext: str) -> str:
    """
    卡密指纹，用于导入去重。

    用 SHA-256 而不是明文比较：即使指纹库泄露，也无法反推出卡密。
    """
    return hashlib.sha256(plaintext.strip().encode("utf-8")).hexdigest()
