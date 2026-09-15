"""
卡密加密测试

重点验证三件事：密文不可逆、跨店解不开、被篡改能发现。
"""

from __future__ import annotations

import base64

import pytest

from backend import guardrails
from backend.crypto import (
    KEY_BYTES,
    DecryptionError,
    KeyMaterialMissing,
    LocalKeyProvider,
    SealedToken,
    configure_provider,
    content_fingerprint,
    derive_store_key,
    generate_master_key,
    load_master_key,
    mask,
)

MASTER = generate_master_key()
PROVIDER = LocalKeyProvider(MASTER)


# ===========================================================================
# 一、主密钥
# ===========================================================================

def test_generate_and_load_roundtrip():
    raw = generate_master_key()
    assert len(load_master_key(raw)) == KEY_BYTES


def test_load_accepts_hex_too():
    raw = generate_master_key()
    as_hex = load_master_key(raw).hex()
    assert load_master_key(as_hex) == load_master_key(raw)


def test_load_rejects_garbage():
    with pytest.raises(KeyMaterialMissing):
        load_master_key("这不是密钥")
    with pytest.raises(KeyMaterialMissing):
        load_master_key("")


def test_provider_rejects_short_key():
    with pytest.raises(KeyMaterialMissing):
        LocalKeyProvider(b"too-short")


# ===========================================================================
# 二、密钥派生
# ===========================================================================

def test_derivation_is_deterministic():
    master = load_master_key(MASTER)
    assert derive_store_key(master, "s1") == derive_store_key(master, "s1")


def test_different_stores_get_different_keys():
    master = load_master_key(MASTER)
    assert derive_store_key(master, "s1") != derive_store_key(master, "s2")


def test_derivation_requires_key_ref():
    with pytest.raises(KeyMaterialMissing):
        derive_store_key(load_master_key(MASTER), "")


# ===========================================================================
# 三、加解密
# ===========================================================================

def test_encrypt_decrypt_roundtrip():
    token = PROVIDER.encrypt("A7F2-9K3M-XQ81", "s1")
    assert PROVIDER.decrypt(token) == "A7F2-9K3M-XQ81"


def test_plaintext_is_not_in_the_token():
    token = PROVIDER.encrypt("SECRET-CARD-1234", "s1")
    assert "SECRET" not in token
    assert "1234" not in token


def test_same_plaintext_encrypts_differently_each_time():
    a = PROVIDER.encrypt("SAME", "s1")
    b = PROVIDER.encrypt("SAME", "s1")
    assert a != b          # 每次随机 nonce，避免"相同卡密"从密文就能看出来


def test_decrypt_with_wrong_key_ref_is_rejected():
    token = PROVIDER.encrypt("CARD", "s1")
    with pytest.raises(DecryptionError):
        PROVIDER.decrypt(token, key_ref="s2")


def test_token_records_which_store_it_belongs_to():
    token = PROVIDER.encrypt("CARD", "s1")
    assert SealedToken.parse(token).key_ref == "s1"


def test_tampered_ciphertext_is_detected():
    token = PROVIDER.encrypt("CARD-VALUE", "s1")
    sealed = SealedToken.parse(token)
    broken = bytearray(sealed.ciphertext)
    broken[0] ^= 0x01
    tampered = SealedToken(key_ref=sealed.key_ref, nonce=sealed.nonce,
                           ciphertext=bytes(broken)).to_str()
    with pytest.raises(DecryptionError):
        PROVIDER.decrypt(tampered)


def test_wrong_master_key_cannot_decrypt():
    token = PROVIDER.encrypt("CARD", "s1")
    other = LocalKeyProvider(generate_master_key())
    with pytest.raises(DecryptionError):
        other.decrypt(token)


def test_reencrypt_moves_a_card_to_another_store():
    token = PROVIDER.encrypt("CARD", "s1")
    moved = PROVIDER.reencrypt(token, "s2")
    assert SealedToken.parse(moved).key_ref == "s2"
    assert PROVIDER.decrypt(moved) == "CARD"


def test_malformed_tokens_are_rejected():
    for bad in ["", "abc", "v1.s1.onlythree", "v9.s1.aGk.aGk"]:
        with pytest.raises(DecryptionError):
            SealedToken.parse(bad)


def test_key_ref_cannot_contain_dot():
    with pytest.raises(ValueError):
        SealedToken(key_ref="a.b", nonce=b"0" * 12, ciphertext=b"x").to_str()


# ===========================================================================
# 四、展示与指纹
# ===========================================================================

def test_mask_keeps_head_and_tail():
    secret = "A7F2-9K3M-XQ81-2ZP4"          # 19 位
    masked = mask(secret)
    assert masked.startswith("A7F2") and masked.endswith("2ZP4")
    assert masked.count("*") == len(secret) - 8


def test_mask_short_secret_is_fully_hidden():
    # 6 位卡密保留 4+4 就等于全泄露了
    assert mask("ABC123") == "******"
    assert mask("ABCDEFGH") == "********"


def test_mask_empty():
    assert mask("") == ""


def test_fingerprint_is_stable_and_irreversible():
    assert content_fingerprint("CARD-1") == content_fingerprint("CARD-1 ")
    assert content_fingerprint("CARD-1") != content_fingerprint("CARD-2")
    assert "CARD-1" not in content_fingerprint("CARD-1")


# ===========================================================================
# 五、与发货路径的集成
# ===========================================================================

def test_guardrails_encrypt_decrypt_via_provider():
    configure_provider(PROVIDER)
    token = guardrails.encrypt("SHIP-ME", "s1")
    assert guardrails.decrypt(token, key_ref="s1") == "SHIP-ME"


def test_guardrails_decrypt_refuses_cross_store():
    configure_provider(PROVIDER)
    token = guardrails.encrypt("SHIP-ME", "s1")
    with pytest.raises(DecryptionError):
        guardrails.decrypt(token, key_ref="s2")


def test_guardrails_reexports_fingerprint():
    assert guardrails.content_fingerprint("X") == content_fingerprint("X")
