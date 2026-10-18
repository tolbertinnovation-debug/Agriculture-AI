"""API keys for partners, signed session tokens for farmers, one-time passwords."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from .config import get_settings

API_KEY_PREFIX = "agk_"


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return ``(plaintext, prefix, hash)``. The plaintext is shown once and never stored."""
    plaintext = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return plaintext, plaintext[:12], hash_secret(plaintext)


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign_token(payload: dict, ttl_seconds: int) -> str:
    body = dict(payload, exp=int(time.time()) + ttl_seconds)
    raw = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(get_settings().secret_key.encode(), raw.encode(), hashlib.sha256).digest()
    return f"{raw}.{_b64(sig)}"


def verify_token(token: str) -> dict | None:
    try:
        raw, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(get_settings().secret_key.encode(), raw.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(expected, _unb64(sig)):
            return None
        payload = json.loads(_unb64(raw))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def constant_time_equals(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode(), b.encode())
