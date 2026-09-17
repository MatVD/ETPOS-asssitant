from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import settings


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def app_db() -> Iterator[sqlite3.Connection]:
    conn = _connect(settings.app_db)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def docs_db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(path or settings.docs_db)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_app_db() -> None:
    settings.ensure_dirs()
    with app_db() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                csrf_token TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                remote_ip TEXT NOT NULL,
                success INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_login_attempts_created ON login_attempts(created_at);
            CREATE INDEX IF NOT EXISTS idx_login_attempts_user ON login_attempts(username, created_at);

            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL DEFAULT 'Nouvelle conversation',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                citations_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
            """
        )


def init_docs_db(path: Path | None = None) -> None:
    target = path or settings.docs_db
    target.parent.mkdir(parents=True, exist_ok=True)
    with docs_db(target) as conn:
        journal_mode = str(conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]).lower()
        if journal_mode != "delete":
            raise sqlite3.OperationalError(
                f"Impossible de basculer {target} en journal_mode=DELETE (mode={journal_mode})"
            )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_priority INTEGER NOT NULL DEFAULT 0,
                language TEXT NOT NULL DEFAULT 'fr',
                detected_version TEXT,
                detected_revision_date TEXT,
                retrieved_at TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                snapshot_path TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                section_order INTEGER NOT NULL,
                title TEXT NOT NULL,
                heading_path TEXT NOT NULL,
                anchor TEXT,
                source_url TEXT NOT NULL,
                source_text TEXT NOT NULL,
                search_text TEXT NOT NULL,
                image_refs_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_sections_doc_order ON sections(document_id, section_order);

            CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(
                title,
                heading_path,
                search_text,
                content='sections',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            );

            CREATE TRIGGER IF NOT EXISTS sections_ai AFTER INSERT ON sections BEGIN
                INSERT INTO sections_fts(rowid, title, heading_path, search_text)
                VALUES (new.id, new.title, new.heading_path, new.search_text);
            END;

            CREATE TRIGGER IF NOT EXISTS sections_ad AFTER DELETE ON sections BEGIN
                INSERT INTO sections_fts(sections_fts, rowid, title, heading_path, search_text)
                VALUES ('delete', old.id, old.title, old.heading_path, old.search_text);
            END;

            CREATE TRIGGER IF NOT EXISTS sections_au AFTER UPDATE ON sections BEGIN
                INSERT INTO sections_fts(sections_fts, rowid, title, heading_path, search_text)
                VALUES ('delete', old.id, old.title, old.heading_path, old.search_text);
                INSERT INTO sections_fts(rowid, title, heading_path, search_text)
                VALUES (new.id, new.title, new.heading_path, new.search_text);
            END;
            """
        )


def init_all() -> None:
    init_app_db()
    init_docs_db()
