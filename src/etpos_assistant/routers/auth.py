from __future__ import annotations

from pathlib import Path

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import settings
from ..db import app_db
from ..security import (
    SESSION_COOKIE,
    client_ip,
    create_session,
    delete_session,
    hash_password,
    needs_rehash,
    verify_password,
)
from .deps import current_session, require_csrf

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _remote_ip(request: Request) -> str:
    return client_ip(request)


def _rate_limited(username: str, remote_ip: str) -> bool:
    since = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
    with app_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM login_attempts
            WHERE success = 0 AND created_at >= ? AND (username = ? COLLATE NOCASE OR remote_ip = ?)
            """,
            (since, username, remote_ip),
        ).fetchone()
    return int(row["n"]) >= 8


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_session(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()[:120]
    remote_ip = _remote_ip(request)
    if _rate_limited(username, remote_ip):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Trop de tentatives. Réessaie plus tard."},
            status_code=429,
        )

    with app_db() as conn:
        user = conn.execute(
            "SELECT id, username, password_hash, is_active FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()

    ok = bool(user and user["is_active"] and verify_password(user["password_hash"], password))
    with app_db() as conn:
        conn.execute(
            "INSERT INTO login_attempts(username, remote_ip, success, created_at) VALUES (?, ?, ?, ?)",
            (username, remote_ip, 1 if ok else 0, _now()),
        )
        conn.execute(
            "DELETE FROM login_attempts WHERE created_at < ?",
            ((datetime.now(UTC) - timedelta(days=2)).isoformat(),),
        )

    if not ok:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Identifiant ou mot de passe incorrect."},
            status_code=401,
        )

    if needs_rehash(user["password_hash"]):
        with app_db() as conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user["id"]))

    token, _csrf, _expires = create_session(user["id"])
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return response


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    session = current_session(request)
    if session:
        require_csrf(request, session, csrf_token)
    delete_session(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
