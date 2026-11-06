"""Xiaomi Health transport crypto (RC4-drop1024 request signing, FDS AES).

Ported from alexgetmancom/miband-bot src/xiaomi/client.ts (GPL-3.0).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import time
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

FDS_AES_IV = b"1234567887654321"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    """Decode standard or URL-safe base64, tolerating missing padding."""
    text = text.replace("-", "+").replace("_", "/")
    return base64.b64decode(text + "=" * (-len(text) % 4))


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def rc4(key: bytes, data: bytes, skip: int = 1024) -> bytes:
    if not key:
        raise ValueError("RC4 key is empty")
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 255
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray(len(data))
    for n in range(skip + len(data)):
        i = (i + 1) & 255
        j = (j + s[i]) & 255
        s[i], s[j] = s[j], s[i]
        if n >= skip:
            out[n - skip] = data[n - skip] ^ s[(s[i] + s[j]) & 255]
    return bytes(out)


def signed_nonce(ssecurity: str, nonce: str) -> str:
    return b64(hashlib.sha256(b64d(ssecurity) + b64d(nonce)).digest())


def make_nonce(random_bytes: bytes | None = None, now_ms: int | None = None) -> str:
    rand = random_bytes if random_bytes is not None else os.urandom(8)
    minutes = (now_ms if now_ms is not None else int(time.time() * 1000)) // 60_000
    return b64(rand + struct.pack(">I", minutes & 0xFFFFFFFF))


def sha1_b64(text: str) -> str:
    return b64(hashlib.sha1(text.encode("utf-8")).digest())


def signature_message(method: str, path: str, params: dict[str, str], snonce: str) -> str:
    path = path if path.startswith("/") else f"/{path}"
    parts = [method.upper(), path, *(f"{k}={params[k]}" for k in sorted(params)), snonce]
    return "&".join(parts)


def js_json(value: Any) -> str:
    """Serialize like JavaScript JSON.stringify (compact, UTF-8 kept)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def encrypted_params(
    method: str, path: str, ssecurity: str, params: dict[str, Any] | None, nonce: str | None = None
) -> dict[str, str]:
    nonce = nonce or make_nonce()
    snonce = signed_nonce(ssecurity, nonce)
    raw: dict[str, str] = {}
    if params is not None:
        raw["data"] = js_json(params)
    raw["rc4_hash__"] = sha1_b64(signature_message(method, path, raw, snonce))
    entries = sorted(raw.items())
    # One RC4 keystream runs across all values concatenated in key order.
    encoded_values = [value.encode("utf-8") for _, value in entries]
    encrypted = rc4(b64d(snonce), b"".join(encoded_values))
    output: dict[str, str] = {}
    position = 0
    for (key, _), value in zip(entries, encoded_values):
        output[key] = b64(encrypted[position : position + len(value)])
        position += len(value)
    output["signature"] = sha1_b64(signature_message(method, path, output, snonce))
    output["_nonce"] = nonce
    return output


def decrypt_response(ssecurity: str, nonce: str, ciphertext: str) -> Any:
    plaintext = rc4(b64d(signed_nonce(ssecurity, nonce)), b64d(ciphertext.strip())).decode("utf-8")
    try:
        return json.loads(plaintext)
    except json.JSONDecodeError:
        return plaintext


def decode_fds_aes(content: bytes, object_key: str) -> bytes:
    key = b64d(object_key)
    # FDS serves base64 text, sometimes as a JSON string literal ("..."), or raw ciphertext.
    text = content.decode("utf-8", errors="ignore").strip().strip('"')
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_+/=-")
    if text and set(text) <= allowed:
        encrypted = b64d(text)
    else:
        encrypted = content
    return unpad(AES.new(key, AES.MODE_CBC, FDS_AES_IV).decrypt(encrypted), AES.block_size)
