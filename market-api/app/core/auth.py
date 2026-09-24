"""认证工具：HS256 JWT 与 PBKDF2 密码哈希（零第三方依赖）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from app.config import settings

_SECRET = settings.auth_secret


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    iterations = 240_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def issue_token(user_id: str, kind: str = "access", ttl: int | None = None) -> str:
    if kind == "refresh":
        exp = int(time.time()) + (ttl or 30 * 24 * 3600)
    else:
        exp = int(time.time()) + (ttl or 2 * 3600)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": user_id, "kind": kind, "exp": exp, "iat": int(time.time())}
    seg = _b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + _b64url(
        json.dumps(payload, separators=(",", ":")).encode()
    )
    sig = hmac.new(_SECRET.encode(), seg.encode(), hashlib.sha256).digest()
    return seg + "." + _b64url(sig)


def verify_token(token: str, expected_kind: str = "access") -> dict | None:
    try:
        seg, sig_text = token.rsplit(".", 1)
        expected_sig = _b64url(hmac.new(_SECRET.encode(), seg.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig_text, expected_sig):
            return None
        header_raw, payload_raw = seg.split(".")
        header = json.loads(_b64url_decode(header_raw))
        if header.get("alg") != "HS256":
            return None
        payload = json.loads(_b64url_decode(payload_raw))
        if payload.get("kind") != expected_kind:
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
