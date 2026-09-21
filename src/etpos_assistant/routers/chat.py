from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
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


class RetryRequest(BaseModel):
    conversation_id: int


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
                raise HTTPException(status_code=404, detail="Conversation introuvable")
            return int(row["id"])
        title = re.sub(r"\s+", " ", message).strip()[:70] or "Nouvelle conversation"
        cursor = conn.execute(
            "INSERT INTO conversations(user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, title, now, now),
        )
        return int(cursor.lastrowid)


def _latest_unanswered_question(user_id: int, conversation_id: int) -> str:
    with app_db() as conn:
        conversation = conn.execute(
            "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation introuvable")

        latest = conn.execute(
            """
            SELECT role, content
            FROM messages
            WHERE conversation_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()

    if not latest or latest["role"] != "user":
        raise HTTPException(
            status_code=409,
            detail="Aucune question sans réponse à réessayer.",
        )
    return str(latest["content"]).strip()


def _streaming_response(
    conversation_id: int,
    question: str,
    *,
    conversation_title: str | None = None,
) -> StreamingResponse:
    async def events():
        meta = {"type": "meta", "conversation_id": conversation_id}
        if conversation_title is not None:
            meta["conversation_title"] = conversation_title
        yield _sse(meta)

        try:
            async for event in stream_chat(conversation_id, question):
                if event.get("type") == "done":
                    save_assistant_message(
                        conversation_id,
                        event["text"],
                        event.get("citations", []),
                        event.get("answer_status"),
                    )
                yield _sse(event)
        except asyncio.CancelledError:
            logger.info("Génération interrompue pour la conversation %s", conversation_id)
            raise
        except Exception:
            logger.exception("Erreur pendant la génération")
            yield _sse({
                "type": "error",
                "message": "La réponse n’a pas pu être générée. Vous pouvez réessayer.",
            })

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat")
async def chat(request: Request, payload: ChatRequest):
    session = require_api_session(request)
    require_csrf(request, session)
    question = payload.message.strip()
    is_new_conversation = payload.conversation_id is None
    conversation_id = _ensure_conversation(
        session["user_id"],
        payload.conversation_id,
        question,
    )
    conversation_title = (
        re.sub(r"\s+", " ", question).strip()[:70] or "Nouvelle conversation"
    )
    now = datetime.now(UTC).isoformat()
    with app_db() as conn:
        conn.execute(
            """
            INSERT INTO messages(
                conversation_id, role, content, citations_json, created_at
            ) VALUES (?, 'user', ?, '[]', ?)
            """,
            (conversation_id, question, now),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )

    return _streaming_response(
        conversation_id,
        question,
        conversation_title=conversation_title if is_new_conversation else None,
    )


@router.post("/chat/retry")
async def retry_chat(request: Request, payload: RetryRequest):
    session = require_api_session(request)
    require_csrf(request, session)
    question = _latest_unanswered_question(
        session["user_id"],
        payload.conversation_id,
    )
    return _streaming_response(payload.conversation_id, question)
