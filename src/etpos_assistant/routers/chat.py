from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..db import app_db
from ..rag import save_assistant_message, stream_chat
from .deps import require_api_session, require_csrf

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: int | None = None


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _ensure_conversation(user_id: int, conversation_id: int | None, message: str) -> int:
    now = datetime.now(UTC).isoformat()
    with app_db() as conn:
        if conversation_id is not None:
            row = conn.execute(
                "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, user_id),
            ).fetchone()
            if not row:
                raise ValueError("Conversation introuvable")
            return int(row["id"])
        title = re.sub(r"\s+", " ", message).strip()[:70] or "Nouvelle conversation"
        cursor = conn.execute(
            "INSERT INTO conversations(user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, title, now, now),
        )
        return int(cursor.lastrowid)


@router.post("/chat")
async def chat(request: Request, payload: ChatRequest):
    session = require_api_session(request)
    require_csrf(request, session)
    question = payload.message.strip()
    conversation_id = _ensure_conversation(session["user_id"], payload.conversation_id, question)
    now = datetime.now(UTC).isoformat()
    with app_db() as conn:
        conn.execute(
            "INSERT INTO messages(conversation_id, role, content, citations_json, created_at) VALUES (?, 'user', ?, '[]', ?)",
            (conversation_id, question, now),
        )
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))

    async def events():
        final: dict | None = None
        yield _sse({"type": "meta", "conversation_id": conversation_id})
        try:
            async for event in stream_chat(conversation_id, question):
                if event.get("type") == "done":
                    final = event
                yield _sse(event)
        except Exception:
            logger.exception("Erreur pendant la génération")
            yield _sse({"type": "error", "message": "La génération a échoué. Réessaie ou vérifie la configuration du provider."})
            return
        if final:
            save_assistant_message(conversation_id, final["text"], final.get("citations", []))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
