"""Razorpay webhook signature. Verify the raw body, never re-serialised JSON."""

from __future__ import annotations

import hmac
import hashlib


def sign(raw_body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    if not signature or not secret:
        return False
    expected = sign(raw_body, secret)
    return hmac.compare_digest(expected, signature)
