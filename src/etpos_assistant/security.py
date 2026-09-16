from __future__ import annotations

import hashlib
import ipaddress
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from fastapi import Request
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError

from .config import settings
from .db import app_db

PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)
SESSION_COOKIE = "__Host-etpos_session" if settings.cookie_secure else "etpos_session"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _peer_is_loopback(request: Request) -> bool:
    if not request.client:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if _peer_is_loopback(request):
        forwarded = request.headers.get("x-real-ip", "").strip()
        if forwarded:
            try:
                ipaddress.ip_address(forwarded)
                return forwarded[:64]
            except ValueError:
                pass
    return peer[:64]


def same_origin_request(request: Request) -> bool:
    if request.method.upper() not in UNSAFE_METHODS:
        return True

    source = (request.headers.get("origin") or request.headers.get("referer") or "").strip()
    if not source:
        return not settings.is_production

    parsed = urlsplit(source)
    source_origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}" if parsed.scheme and parsed.netloc else ""
    if not source_origin:
        return False

    if settings.public_origin:
        return source_origin == settings.public_origin.lower()

    scheme = request.url.scheme
    if _peer_is_loopback(request):
        forwarded_proto = request.headers.get("x-forwarded-proto", "").strip().lower()
        if forwarded_proto in {"http", "https"}:
            scheme = forwarded_proto
    host = request.headers.get("host", "").strip().lower()
    return bool(host) and source_origin == f"{scheme.lower()}://{host}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def hash_password(password: str) -> str:
    return PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return PASSWORD_HASHER.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def create_session(user_id: int) -> tuple[str, str, str]:
    raw_token = secrets.token_urlsafe(48)
    csrf = new_csrf_token()
    now = utcnow()
    expires = now + timedelta(hours=settings.session_hours)
    with app_db() as conn:
        conn.execute(
            """
            INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, expires_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (_token_hash(raw_token), user_id, csrf, iso(now), iso(expires), iso(now)),
        )
    return raw_token, csrf, iso(expires)


def delete_session(raw_token: str | None) -> None:
    if not raw_token:
        return
    with app_db() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(raw_token),))


def get_session(raw_token: str | None):
    if not raw_token:
        return None
    now = iso(utcnow())
    with app_db() as conn:
        row = conn.execute(
            """
            SELECT s.id AS session_id, s.csrf_token, s.expires_at,
                   u.id AS user_id, u.username, u.is_active
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1
            """,
            (_token_hash(raw_token), now),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
                (now, row["session_id"]),
            )
        return row


def secure_compare(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return secrets.compare_digest(a, b)
