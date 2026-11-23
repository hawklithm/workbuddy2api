"""Trae state.vscdb credential decryption.

Reverse engineered from Trae CN 3.4.4 out-build/vs/base/common/byteCrypto.js.

Scheme (function H1/YVe encrypt, b2/GVe decrypt):
  header = bytes([116, 99, 5, 16, 0, 0]) + 32-byte random key
  payload= AES-128-CBC( SHA512(plaintext)[:64] + plaintext, key, iv )
  key/iv derivation (Fle):
    buf = 128 bytes
    buf[0:64]  = SHA512(key)
    buf[64:128]= XOR_TABLES (Gle[i] ^ zle[i], 64 bytes)
    buf[0:64]  = SHA512(buf)           # overwrite
    aesKey = buf[0:16], iv = buf[16:32]
  value stored base64(header+payload) in ItemTable under the storage key.
"""
from __future__ import annotations

import base64
import hashlib
import sqlite3
import sys

HEADER = bytes([116, 99, 5, 16, 0, 0])  # "tc" v5 AES

GLE = bytes([82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
             124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
             84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
             8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37])
ZLE = bytes([31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
             96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
             160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
             23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125])
XOR_TABLES = bytes(g ^ z for g, z in zip(GLE, ZLE))

KEY_LEN = 32       # S2
HASH_PREFIX = 64   # Qh (SHA512)
HEADER_LEN = 6     # W1


def derive_key_iv(key: bytes, version: bytes = b"AES") -> tuple[bytes, bytes]:
    buf = bytearray(128)                      # Qh + PN = 64 + 64
    buf[0:64] = hashlib.sha512(key).digest()
    if version == b"AES":
        buf[64:128] = XOR_TABLES
    else:  # AES_PRIVATE variant (Vle ^ qle)
        raise ValueError("AES_PRIVATE variant not needed here")
    buf[0:64] = hashlib.sha512(bytes(buf)).digest()
    return bytes(buf[0:16]), bytes(buf[16:32])  # AES128 key + 16-byte IV


def decrypt_blob(data: bytes) -> bytes | None:
    if len(data) < HEADER_LEN + KEY_LEN + 16:
        return None
    if data[0:6] != HEADER:
        return None
    key = data[6:38]
    aes_key, iv = derive_key_iv(key)

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    dec = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded = dec.update(data[38:]) + dec.finalize()
    # strip PKCS7
    pad = padded[-1]
    if not 1 <= pad <= 16 or padded[-pad:] != bytes([pad]) * pad:
        return None
    plain = padded[:-pad]
    if len(plain) < HASH_PREFIX:
        return None
    digest, payload = plain[:HASH_PREFIX], plain[HASH_PREFIX:]
    if hashlib.sha512(payload).digest() != digest:
        return None
    return payload


def decrypt_value(b64_value: str) -> str | None:
    try:
        raw = base64.b64decode(b64_value)
    except Exception:
        return None
    out = decrypt_blob(raw)
    return out.decode("utf-8", "replace") if out is not None else None


def dump_state(db_path: str, only_keys: list[str] | None = None) -> dict[str, str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT key, value FROM ItemTable").fetchall()
    finally:
        con.close()
    result: dict[str, str] = {}
    for key, value in rows:
        if not isinstance(value, str):
            continue
        if only_keys and not any(k in key for k in only_keys):
            continue
        plain = decrypt_value(value)
        if plain is not None:
            result[key] = plain
    return result


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "notes/state-copy.vscdb"
    wanted = sys.argv[2].split(",") if len(sys.argv) > 2 else ["iCubeAuthInfo"]
    found = dump_state(db, wanted)
    for k, v in found.items():
        print(f"===== {k} ({len(v)} chars) =====")
        print(v if len(v) < 4000 else v[:4000] + f"...(+{len(v)-4000})")
    if not found:
        print("No decryptable values for:", wanted)
