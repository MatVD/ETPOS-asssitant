from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..config import settings
from ..db import app_db, docs_db

router = APIRouter(prefix="/health")


@router.get("/live")
def live():
    return {"status": "ok"}


@router.get("/ready")
def ready():
    errors: list[str] = []
    corpus_sections = 0
    try:
        with app_db() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:
        errors.append("app_db")
    try:
        with docs_db() as conn:
            corpus_sections = int(conn.execute("SELECT COUNT(*) AS n FROM sections").fetchone()["n"])
    except Exception:
        errors.append("docs_db")
    if settings.provider == "codex":
        binary_ok = shutil.which(settings.codex_binary) is not None or Path(settings.codex_binary).is_file()
        if not binary_ok:
            errors.append("codex_cli")
    body = {"status": "ok" if not errors else "not_ready", "corpus_sections": corpus_sections, "errors": errors}
    return JSONResponse(body, status_code=200 if not errors else 503)
