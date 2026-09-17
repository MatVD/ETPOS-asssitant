from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path


REQUIRED_APP_TABLES = {
    "users",
    "sessions",
    "login_attempts",
    "conversations",
    "messages",
}
BACKUP_GLOB = "app-*.db"


class BackupError(RuntimeError):
    pass


def _readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise BackupError(f"Base SQLite introuvable : {path}")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def validate_app_database(path: Path) -> None:
    try:
        with _readonly_connection(path) as conn:
            integrity = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
            if integrity != ["ok"]:
                raise BackupError(f"Échec integrity_check pour {path}: {'; '.join(integrity)}")

            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            missing = sorted(REQUIRED_APP_TABLES - tables)
            if missing:
                raise BackupError(
                    f"Schéma app.db incomplet pour {path}: tables manquantes {', '.join(missing)}"
                )

            foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise BackupError(
                    f"Échec foreign_key_check pour {path}: {len(foreign_key_errors)} erreur(s)"
                )
    except sqlite3.Error as exc:
        raise BackupError(f"Impossible de valider {path}: {exc}") from exc


def _copy_with_sqlite_backup(source: Path, destination: Path) -> None:
    try:
        with _readonly_connection(source) as source_conn:
            with sqlite3.connect(destination, timeout=10) as destination_conn:
                destination_conn.execute("PRAGMA busy_timeout = 5000")
                source_conn.backup(destination_conn, pages=256, sleep=0.05)
                journal_mode = destination_conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
                if str(journal_mode).lower() != "delete":
                    raise BackupError(
                        f"Impossible de rendre la sauvegarde SQLite autonome : journal_mode={journal_mode}"
                    )
    except sqlite3.Error as exc:
        raise BackupError(f"Échec de la sauvegarde SQLite de {source}: {exc}") from exc


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _remove_sqlite_file_set(path: Path) -> None:
    path.unlink(missing_ok=True)
    _remove_sqlite_sidecars(path)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_app_backup(source: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    final_path = backup_dir / f"app-{timestamp}.db"

    fd, raw_temp_path = tempfile.mkstemp(prefix=".app-backup-", suffix=".tmp", dir=backup_dir)
    os.close(fd)
    temp_path = Path(raw_temp_path)

    try:
        _copy_with_sqlite_backup(source, temp_path)
        validate_app_database(temp_path)
        _remove_sqlite_sidecars(temp_path)
        os.chmod(temp_path, 0o600)
        _fsync_file(temp_path)
        os.replace(temp_path, final_path)
        _fsync_directory(backup_dir)
        return final_path
    except Exception:
        _remove_sqlite_file_set(temp_path)
        raise


def prune_app_backups(
    backup_dir: Path,
    retention_days: int,
    *,
    now: datetime | None = None,
) -> list[Path]:
    if retention_days < 1:
        raise BackupError("La rétention doit être d'au moins 1 jour.")
    if not backup_dir.exists():
        return []

    backups = sorted(
        (path for path in backup_dir.glob(BACKUP_GLOB) if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not backups:
        return []

    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    cutoff = reference - timedelta(days=retention_days)

    deleted: list[Path] = []
    for path in backups[1:]:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if modified < cutoff:
            path.unlink()
            deleted.append(path)

    if deleted:
        _fsync_directory(backup_dir)
    return deleted


def restore_app_database(backup_path: Path, target_path: Path) -> Path:
    validate_app_database(backup_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    fd, raw_temp_path = tempfile.mkstemp(
        prefix=f".{target_path.name}.restore-",
        suffix=".tmp",
        dir=target_path.parent,
    )
    os.close(fd)
    temp_path = Path(raw_temp_path)

    try:
        _copy_with_sqlite_backup(backup_path, temp_path)
        validate_app_database(temp_path)
        _remove_sqlite_sidecars(temp_path)
        os.chmod(temp_path, 0o600)
        _fsync_file(temp_path)
        _remove_sqlite_sidecars(target_path)
        os.replace(temp_path, target_path)
        _fsync_directory(target_path.parent)
        return target_path
    except Exception:
        _remove_sqlite_file_set(temp_path)
        raise
