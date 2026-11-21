"""Aha's reviewed cache envelope, for independent local records only.

Derived from pinned native instructions; no native runtime cache paths or
account material are accessed. SHA-512/AES use the existing crypto dependency.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HEADER = bytes.fromhex("746305100000")
LEGACY_HEADER = bytes.fromhex("123920200203")
# XOR of the two public SDK constant tables at 0xc139780/0xc1397c0
# and 0xc139700/0xc139740. These are application constants, not user keys.
_SUFFIX = bytes.fromhex("4dd4c2e6b83162090e52b3c7a6733ba41cb2462b829ab58a196b39db57177524"
                       "f49baf7f08e8d68d26a72e37c1a95a2f1f05a51892aef2949732b62a38aadd58")
_LEGACY_SUFFIX = bytes.fromhex("490cc21292b05d0cc06ccbe91fb92e21b5a7c538811dc85e27a96e28104e1cf6"
                              "9c7166efab232807390da78a8b91e49c44b49a43feb941a1e46f2656e613e47b")


def _seed_bytes(clock: int) -> bytes:
    state = clock & 0xffffffff
    result = bytearray()
    for _ in range(32):
        state = (state * 0x343fd + 0x269ec3) & 0xffffffff
        result.append((state >> 16) & 255)
    return bytes(result)


def _key_iv(seed: bytes, legacy: bool = False) -> tuple[bytes, bytes]:
    digest = hashlib.sha512(hashlib.sha512(seed).digest() + (_LEGACY_SUFFIX if legacy else _SUFFIX)).digest()
    return digest[:16], digest[16:32]


def encrypt(value: bytes, *, clock: int | None = None) -> bytes:
    seed = _seed_bytes(int(time.time()) if clock is None else clock)
    key, iv = _key_iv(seed)
    plain = hashlib.sha512(value).digest() + value
    padding = 16 - len(plain) % 16
    worker = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return HEADER + seed + worker.update(plain + bytes([padding]) * padding) + worker.finalize()


def decrypt(value: bytes) -> bytes:
    if len(value) < 118 or (len(value) - 38) % 16:
        return b""
    header = value[:6]
    if header not in (HEADER, LEGACY_HEADER):
        return b""
    key, iv = _key_iv(value[6:38], header == LEGACY_HEADER)
    worker = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = worker.update(value[38:]) + worker.finalize()
    padding = plain[-1]
    # Native checks the last padding byte, not every PKCS#7 padding byte.
    if not 1 <= padding <= 16:
        return b""
    plain = plain[:-padding]
    if len(plain) <= 64 or not hmac.compare_digest(plain[:64], hashlib.sha512(plain[64:]).digest()):
        return b""
    return plain[64:]


def encode(value: bytes, *, clock: int | None = None) -> str:
    return base64.b64encode(encrypt(value, clock=clock)).decode("ascii")


def decode_base64(value: str) -> bytes | None:
    try:
        raw = value.encode("ascii")
        if len(raw) % 4:
            return None
        return base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error, UnicodeError):
        return None


def decode(value: str) -> bytes:
    raw = decode_base64(value)
    return decrypt(raw) if raw is not None else b""
