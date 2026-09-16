from __future__ import annotations

from fastapi import HTTPException, Request, status

from ..security import SESSION_COOKIE, get_session, secure_compare


def current_session(request: Request):
    return get_session(request.cookies.get(SESSION_COOKIE))


def require_api_session(request: Request):
    session = current_session(request)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentification requise")
    return session


def require_csrf(request: Request, session, supplied: str | None = None) -> None:
    token = supplied or request.headers.get("X-CSRF-Token")
    if not secure_compare(token, session["csrf_token"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Jeton CSRF invalide")
