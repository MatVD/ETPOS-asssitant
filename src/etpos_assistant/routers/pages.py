from __future__ import annotations

from pathlib import Path

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..citations import normalize_citation_link
from ..db import app_db
from ..markdown import render_safe_markdown
from .deps import current_session

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _page_context(request: Request, session, conversation_id: int | None = None) -> dict:
    with app_db() as conn:
        conversations = conn.execute(
            "SELECT id, title, updated_at FROM conversations WHERE user_id = ? ORDER BY updated_at DESC",
            (session["user_id"],),
        ).fetchall()
        messages = []
        conversation_exists = conversation_id is None
        if conversation_id is not None:
            owner = conn.execute(
                "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, session["user_id"]),
            ).fetchone()
            conversation_exists = owner is not None
            if owner:
                rows = conn.execute(
                    """
                    SELECT role, content, citations_json, answer_status
                    FROM messages
                    WHERE conversation_id = ?
                    ORDER BY id
                    """,
                    (conversation_id,),
                ).fetchall()
                for row in rows:
                    citations = [
                        normalize_citation_link(citation)
                        for citation in json.loads(row["citations_json"] or "[]")
                    ]
                    messages.append(
                        {
                            "role": row["role"],
                            "content": row["content"],
                            "html": render_safe_markdown(row["content"]) if row["role"] == "assistant" else None,
                            "citations": citations,
                            "answer_status": row["answer_status"] if row["role"] == "assistant" else None,
                        }
                    )
    return {
        "user": session,
        "csrf_token": session["csrf_token"],
        "conversations": conversations,
        "conversation_id": conversation_id,
        "conversation_exists": conversation_exists,
        "messages": messages,
    }


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    session = current_session(request)
    if not session:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request=request, name="chat.html", context=_page_context(request, session))


@router.get("/c/{conversation_id}", response_class=HTMLResponse)
def conversation(request: Request, conversation_id: int):
    session = current_session(request)
    if not session:
        return RedirectResponse("/login", status_code=303)
    context = _page_context(request, session, conversation_id)
    if not context["conversation_exists"]:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="chat.html", context=context)
