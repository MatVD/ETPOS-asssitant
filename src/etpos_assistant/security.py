from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
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
