from __future__ import annotations

from pathlib import Path

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..db import docs_db
from .deps import current_session

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


@router.get("/sources/{section_id}", response_class=HTMLResponse)
def source_page(request: Request, section_id: int):
    session = current_session(request)
    if not session:
        return RedirectResponse("/login", status_code=303)
    with docs_db() as conn:
        row = conn.execute(
            """
            SELECT s.*, d.name AS document_name, d.detected_version, d.detected_revision_date, d.retrieved_at
            FROM sections s JOIN documents d ON d.id = s.document_id
            WHERE s.id = ?
            """,
            (section_id,),
        ).fetchone()
    if not row:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="source.html",
        context={"user": session, "section": row, "images": json.loads(row["image_refs_json"] or "[]")},
    )
