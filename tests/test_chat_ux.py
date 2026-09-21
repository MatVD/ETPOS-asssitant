from __future__ import annotations

import json
import sqlite3
from contextlib import suppress
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import etpos_assistant.db as db_module
import etpos_assistant.routers.chat as chat_module
from etpos_assistant.routers.pages import _page_context


def _settings(tmp_path):
    data_dir = tmp_path / "data"
    return SimpleNamespace(
        data_dir=data_dir,
        app_db=data_dir / "app.db",
        ensure_dirs=lambda: data_dir.mkdir(parents=True, exist_ok=True),
    )


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/api/chat",
            "raw_path": b"/api/chat",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8787),
        }
    )


def test_init_app_db_adds_answer_status_to_legacy_messages(monkeypatch, tmp_path):
    test_settings = _settings(tmp_path)
    monkeypatch.setattr(db_module, "settings", test_settings)
    test_settings.ensure_dirs()

    with sqlite3.connect(test_settings.app_db) as conn:
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                citations_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            )
            """
        )

    db_module.init_app_db()

    with db_module.app_db() as conn:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }

    assert "answer_status" in columns


def test_page_context_keeps_full_conversation_history_accessible(monkeypatch, tmp_path):
    monkeypatch.setattr(db_module, "settings", _settings(tmp_path))
    db_module.init_app_db()

    with db_module.app_db() as conn:
        user_id = conn.execute(
            "INSERT INTO users(username, password_hash, is_active, created_at) VALUES (?, ?, 1, ?)",
            ("tester", "unused", "2026-09-21T00:00:00+00:00"),
        ).lastrowid
        first_id = None
        for index in range(51):
            conversation_id = conn.execute(
                "INSERT INTO conversations(user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (
                    user_id,
                    f"Conversation {index}",
                    f"2026-09-21T00:{index:02d}:00+00:00",
                    f"2026-09-21T00:{index:02d}:00+00:00",
                ),
            ).lastrowid
            if first_id is None:
                first_id = conversation_id
        conn.execute(
            "INSERT INTO messages(conversation_id, role, content, citations_json, created_at) VALUES (?, 'user', ?, '[]', ?)",
            (first_id, "Ancienne question", "2026-09-21T00:00:01+00:00"),
        )
        conn.execute(
            """
            INSERT INTO messages(
                conversation_id, role, content, citations_json, answer_status, created_at
            ) VALUES (?, 'assistant', ?, '[]', 'partial', ?)
            """,
            (first_id, "Réponse partielle", "2026-09-21T00:00:02+00:00"),
        )

    context = _page_context(
        _request(),
        {"user_id": user_id, "username": "tester", "csrf_token": "csrf"},
        int(first_id),
    )

    assert len(context["conversations"]) == 51
    assert any(int(row["id"]) == int(first_id) for row in context["conversations"])
    assert context["conversation_exists"] is True
    assert [message["content"] for message in context["messages"]] == [
        "Ancienne question",
        "Réponse partielle",
    ]
    assert context["messages"][1]["answer_status"] == "partial"
    assert context["pending_response"] is False


@pytest.mark.asyncio
async def test_retry_reuses_latest_unanswered_question_without_duplicate(monkeypatch, tmp_path):
    monkeypatch.setattr(db_module, "settings", _settings(tmp_path))
    db_module.init_app_db()

    with db_module.app_db() as conn:
        user_id = conn.execute(
            "INSERT INTO users(username, password_hash, is_active, created_at) VALUES (?, ?, 1, ?)",
            ("tester", "unused", "2026-09-21T00:00:00+00:00"),
        ).lastrowid
        conversation_id = conn.execute(
            "INSERT INTO conversations(user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (
                user_id,
                "Sauvegarde",
                "2026-09-21T00:00:00+00:00",
                "2026-09-21T00:00:00+00:00",
            ),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO messages(
                conversation_id, role, content, citations_json, created_at
            ) VALUES (?, 'user', ?, '[]', ?)
            """,
            (
                conversation_id,
                "Comment sauvegarder ?",
                "2026-09-21T00:00:01+00:00",
            ),
        )

    monkeypatch.setattr(
        chat_module,
        "require_api_session",
        lambda _request: {"user_id": user_id, "username": "tester", "csrf_token": "csrf"},
    )
    monkeypatch.setattr(chat_module, "require_csrf", lambda _request, _session: None)

    captured = {}

    async def fake_stream_chat(received_conversation_id, question):
        captured["conversation_id"] = received_conversation_id
        captured["question"] = question
        yield {
            "type": "done",
            "text": "Réponse finale",
            "html": "<p>Réponse finale</p>",
            "citations": [],
            "answer_status": "full",
        }

    monkeypatch.setattr(chat_module, "stream_chat", fake_stream_chat)

    response = await chat_module.retry_chat(
        _request(),
        chat_module.RetryRequest(conversation_id=int(conversation_id)),
    )
    async for _chunk in response.body_iterator:
        pass

    with db_module.app_db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()

    assert captured == {
        "conversation_id": int(conversation_id),
        "question": "Comment sauvegarder ?",
    }
    assert [(row["role"], row["content"]) for row in rows] == [
        ("user", "Comment sauvegarder ?"),
        ("assistant", "Réponse finale"),
    ]


@pytest.mark.asyncio
async def test_retry_rejects_conversation_with_completed_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(db_module, "settings", _settings(tmp_path))
    db_module.init_app_db()

    with db_module.app_db() as conn:
        user_id = conn.execute(
            "INSERT INTO users(username, password_hash, is_active, created_at) VALUES (?, ?, 1, ?)",
            ("tester", "unused", "2026-09-21T00:00:00+00:00"),
        ).lastrowid
        conversation_id = conn.execute(
            "INSERT INTO conversations(user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (
                user_id,
                "Sauvegarde",
                "2026-09-21T00:00:00+00:00",
                "2026-09-21T00:00:00+00:00",
            ),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO messages(
                conversation_id, role, content, citations_json, created_at
            ) VALUES (?, 'user', ?, '[]', ?)
            """,
            (
                conversation_id,
                "Comment sauvegarder ?",
                "2026-09-21T00:00:01+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO messages(
                conversation_id, role, content, citations_json, answer_status, created_at
            ) VALUES (?, 'assistant', ?, '[]', 'full', ?)
            """,
            (
                conversation_id,
                "Réponse finale",
                "2026-09-21T00:00:02+00:00",
            ),
        )

    monkeypatch.setattr(
        chat_module,
        "require_api_session",
        lambda _request: {"user_id": user_id, "username": "tester", "csrf_token": "csrf"},
    )
    monkeypatch.setattr(chat_module, "require_csrf", lambda _request, _session: None)

    with pytest.raises(HTTPException) as exc_info:
        await chat_module.retry_chat(
            _request(),
            chat_module.RetryRequest(conversation_id=int(conversation_id)),
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_interrupted_chat_does_not_persist_partial_assistant(monkeypatch, tmp_path):
    monkeypatch.setattr(db_module, "settings", _settings(tmp_path))
    db_module.init_app_db()

    with db_module.app_db() as conn:
        user_id = conn.execute(
            "INSERT INTO users(username, password_hash, is_active, created_at) VALUES (?, ?, 1, ?)",
            ("tester", "unused", "2026-09-21T00:00:00+00:00"),
        ).lastrowid

    monkeypatch.setattr(
        chat_module,
        "require_api_session",
        lambda _request: {"user_id": user_id, "username": "tester", "csrf_token": "csrf"},
    )
    monkeypatch.setattr(chat_module, "require_csrf", lambda _request, _session: None)

    async def fake_stream_chat(_conversation_id, _question):
        yield {"type": "status", "stage": "searching", "text": "Recherche…"}
        yield {"type": "delta", "text": "Réponse partielle"}

    monkeypatch.setattr(chat_module, "stream_chat", fake_stream_chat)

    response = await chat_module.chat(
        _request(),
        chat_module.ChatRequest(message="Comment sauvegarder ?", conversation_id=None),
    )

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)

    conversation_id = None
    for chunk in chunks:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        with suppress(json.JSONDecodeError):
            payload = json.loads(text.removeprefix("data: ").strip())
            if payload.get("type") == "meta":
                conversation_id = int(payload["conversation_id"])

    assert conversation_id is not None
    with db_module.app_db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()

    assert [(row["role"], row["content"]) for row in rows] == [
        ("user", "Comment sauvegarder ?"),
    ]

    context = _page_context(
        _request(),
        {"user_id": user_id, "username": "tester", "csrf_token": "csrf"},
        conversation_id,
    )
    assert context["pending_response"] is True


@pytest.mark.asyncio
async def test_chat_persists_assistant_before_done_event(monkeypatch, tmp_path):
    monkeypatch.setattr(db_module, "settings", _settings(tmp_path))
    db_module.init_app_db()

    with db_module.app_db() as conn:
        user_id = conn.execute(
            "INSERT INTO users(username, password_hash, is_active, created_at) VALUES (?, ?, 1, ?)",
            ("tester", "unused", "2026-09-21T00:00:00+00:00"),
        ).lastrowid

    monkeypatch.setattr(
        chat_module,
        "require_api_session",
        lambda _request: {"user_id": user_id, "username": "tester", "csrf_token": "csrf"},
    )
    monkeypatch.setattr(chat_module, "require_csrf", lambda _request, _session: None)

    async def fake_stream_chat(_conversation_id, _question):
        yield {"type": "status", "stage": "searching", "text": "Recherche…"}
        yield {
            "type": "done",
            "text": "Réponse [S1]",
            "html": "<p>Réponse [S1]</p>",
            "citations": [],
            "answer_status": "partial",
        }

    monkeypatch.setattr(chat_module, "stream_chat", fake_stream_chat)

    persisted = {"done": False}

    def fake_save(_conversation_id, text, citations, answer_status):
        assert text == "Réponse [S1]"
        assert citations == []
        assert answer_status == "partial"
        persisted["done"] = True

    monkeypatch.setattr(chat_module, "save_assistant_message", fake_save)

    response = await chat_module.chat(
        _request(),
        chat_module.ChatRequest(message="Comment sauvegarder ?", conversation_id=None),
    )

    saw_meta = False
    saw_done = False
    async for chunk in response.body_iterator:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        with suppress(json.JSONDecodeError):
            payload = json.loads(text.removeprefix("data: ").strip())
            if payload.get("type") == "meta":
                saw_meta = True
                assert payload["conversation_title"] == "Comment sauvegarder ?"
            if payload.get("type") == "done":
                assert persisted["done"] is True
                saw_done = True

    assert saw_meta is True
    assert saw_done is True
