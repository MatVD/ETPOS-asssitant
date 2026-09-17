from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from etpos_assistant.backups import (
    BackupError,
    create_app_backup,
    prune_app_backups,
    restore_app_database,
    validate_app_database,
)


def _create_app_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        PRAGMA foreign_keys = ON;
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY,
            token_hash TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf_token TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE login_attempts (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            remote_ip TEXT NOT NULL,
            success INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            citations_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO users VALUES (1, 'mat', 'hash', 1, '2026-09-17T08:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO conversations VALUES (1, 1, 'Test', '2026-09-17T08:00:00+00:00', '2026-09-17T08:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO messages VALUES (1, 1, 'user', 'premier message', '[]', '2026-09-17T08:00:00+00:00')"
    )
    conn.commit()
    return conn


def test_backup_is_consistent_while_wal_database_is_open(tmp_path):
    source = tmp_path / "app.db"
    writer = _create_app_db(source)
    try:
        backup_dir = tmp_path / "backups"
        backup = create_app_backup(source, backup_dir)
        assert backup.is_file()
        assert backup.stat().st_mode & 0o777 == 0o600
        validate_app_database(backup)
        with sqlite3.connect(backup) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert not list(backup_dir.glob(".app-backup-*"))
        assert not backup.with_name(backup.name + "-wal").exists()
        assert not backup.with_name(backup.name + "-shm").exists()

        writer.execute(
            "INSERT INTO messages VALUES (2, 1, 'assistant', 'après backup', '[]', '2026-09-17T08:01:00+00:00')"
        )
        writer.commit()

        with sqlite3.connect(backup) as conn:
            contents = [row[0] for row in conn.execute("SELECT content FROM messages ORDER BY id")]
        assert contents == ["premier message"]
    finally:
        writer.close()


def test_restore_rebuilds_standalone_database_from_backup(tmp_path):
    source = tmp_path / "app.db"
    writer = _create_app_db(source)
    writer.close()
    backup = create_app_backup(source, tmp_path / "backups")

    restore_dir = tmp_path / "restore"
    target = restore_dir / "app.db"
    restore_dir.mkdir()
    target.with_name(target.name + "-wal").write_bytes(b"stale wal")
    target.with_name(target.name + "-shm").write_bytes(b"stale shm")

    restored = restore_app_database(backup, target)
    validate_app_database(restored)

    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT username FROM users").fetchone()[0] == "mat"
        assert conn.execute("SELECT content FROM messages").fetchone()[0] == "premier message"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    assert not list(restore_dir.glob(".app.db.restore-*"))
    assert not restored.with_name(restored.name + "-wal").exists()
    assert not restored.with_name(restored.name + "-shm").exists()


def test_invalid_database_is_rejected(tmp_path):
    invalid = tmp_path / "invalid.db"
    with sqlite3.connect(invalid) as conn:
        conn.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")

    with pytest.raises(BackupError, match="tables manquantes"):
        validate_app_database(invalid)


def test_retention_deletes_expired_backups_but_keeps_latest(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    now = datetime(2026, 9, 17, 8, 0, tzinfo=UTC)

    recent = backup_dir / "app-recent.db"
    expired = backup_dir / "app-expired.db"
    recent.write_bytes(b"recent")
    expired.write_bytes(b"expired")
    os.utime(recent, (now.timestamp(), now.timestamp()))
    old = now - timedelta(days=31)
    os.utime(expired, (old.timestamp(), old.timestamp()))

    deleted = prune_app_backups(backup_dir, 30, now=now)

    assert deleted == [expired]
    assert recent.exists()
    assert not expired.exists()
