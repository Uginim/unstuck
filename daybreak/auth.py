"""Signed order links (whoever holds the link has access; this is not identity verification)
and a seller session cookie. Web routes use require_order and require_seller."""
import base64
import hashlib
import hmac
import os
import time

from fastapi import Cookie, HTTPException

SELLER_COOKIE = "daybreak_seller"
SELLER_SESSION_SECONDS = 12 * 3600


def _secret() -> bytes:
    secret = os.environ.get("DAYBREAK_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("DAYBREAK_SECRET must be set to at least 32 characters")
    return secret.encode()


def _sign(payload: str) -> str:
    digest = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _make(subject: str, expires_at: int) -> str:
    payload = f"{subject}.{expires_at}"
    return f"{payload}.{_sign(payload)}"


def _read(token: str) -> str | None:
    try:
        subject, expires, signature = token.rsplit(".", 2)
        expires_at = int(expires)
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _sign(f"{subject}.{expires}")) or expires_at <= time.time():
        return None
    return subject


def make_order_token(order_id: str, expires_at: int) -> str:
    return _make(f"order:{order_id}", expires_at)


def require_order(token: str) -> str:
    """Path dependency. Returns the order id; the same 404 covers invalid and expired links."""
    subject = _read(token)
    if not subject or not subject.startswith("order:"):
        raise HTTPException(404)
    return subject.removeprefix("order:")


def check_seller_password(password: str) -> bool:
    expected = os.environ.get("DAYBREAK_SELLER_PASSWORD", "")
    return bool(expected) and hmac.compare_digest(password.encode(), expected.encode())


def make_seller_cookie() -> str:
    return _make("seller", int(time.time()) + SELLER_SESSION_SECONDS)


def require_seller(daybreak_seller: str | None = Cookie(default=None)) -> None:
    """Cookie must be set HttpOnly, SameSite=Strict, and Secure outside local runs."""
    if not daybreak_seller or _read(daybreak_seller) != "seller":
        raise HTTPException(401)
