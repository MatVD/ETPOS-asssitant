from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .config import settings
from .docs_versions import INDEX_VERSION, PARSER_VERSION


class DocsDatabaseError(RuntimeError):
    pass


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal"))


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_docs_database(path: Path, *, require_delete_journal: bool = True) -> dict[str, int | str]:
    path = path.resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise DocsDatabaseError(f"Base documentaire absente ou vide : {path}")

    if require_delete_journal:
        existing_sidecars = [sidecar.name for sidecar in _sidecars(path) if sidecar.exists()]
        if existing_sidecars:
            raise DocsDatabaseError(
                f"La base documentaire possède des sidecars SQLite incompatibles avec une activation atomique : {', '.join(existing_sidecars)}"
            )

    try:
        with _readonly_connection(path) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchall()
            if [row[0] for row in integrity] != ["ok"]:
                raise DocsDatabaseError(f"PRAGMA integrity_check a échoué pour {path}")

            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            required = {"documents", "sections", "sections_fts"}
            missing = sorted(required - tables)
            if missing:
                raise DocsDatabaseError(
                    f"Schéma documentaire incomplet dans {path}: {', '.join(missing)}"
                )

            documents = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
            sections = int(conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0])
            fts_rows = int(conn.execute("SELECT COUNT(*) FROM sections_fts").fetchone()[0])
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

            if documents <= 0 or sections <= 0:
                raise DocsDatabaseError(f"Corpus documentaire vide dans {path}")
            if fts_rows != sections:
                raise DocsDatabaseError(
                    f"Index FTS incohérent dans {path}: sections={sections}, fts={fts_rows}"
                )
            if require_delete_journal and journal_mode != "delete":
                raise DocsDatabaseError(
                    f"journal_mode={journal_mode} pour {path}; DELETE est requis pour l'activation atomique"
                )
    except sqlite3.Error as exc:
        raise DocsDatabaseError(f"Validation SQLite impossible pour {path}: {exc}") from exc

    return {
        "documents": documents,
        "sections": sections,
        "fts_rows": fts_rows,
        "journal_mode": journal_mode,
    }


def read_build_metadata(path: Path | None = None) -> dict[str, int | str] | None:
    database = (path or settings.docs_db).resolve()
    if not database.is_file():
        return None
    try:
        with _readonly_connection(database) as conn:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            if "build_metadata" not in tables:
                return None
            row = conn.execute(
                "SELECT parser_version, index_version, built_at FROM build_metadata WHERE id = 1"
            ).fetchone()
    except sqlite3.Error as exc:
        raise DocsDatabaseError(f"Lecture des métadonnées de build impossible : {exc}") from exc
    return dict(row) if row else None


def write_build_metadata(path: Path) -> None:
    from .db import docs_db

    with docs_db(path) as conn:
        conn.execute(
            """
            INSERT INTO build_metadata(id, parser_version, index_version, built_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                parser_version=excluded.parser_version,
                index_version=excluded.index_version,
                built_at=excluded.built_at
            """,
            (PARSER_VERSION, INDEX_VERSION, datetime.now(UTC).isoformat()),
        )


def build_versions_match(path: Path | None = None) -> bool:
    metadata = read_build_metadata(path)
    return bool(
        metadata
        and metadata["parser_version"] == PARSER_VERSION
        and metadata["index_version"] == INDEX_VERSION
    )


def document_manifest(path: Path | None = None) -> dict[str, dict]:
    database = (path or settings.docs_db).resolve()
    if not database.is_file():
        return {}
    try:
        with _readonly_connection(database) as conn:
            rows = conn.execute(
                """
                SELECT source_key, name, source_url, detected_version, detected_revision_date,
                       retrieved_at, content_hash, snapshot_path
                FROM documents
                """
            ).fetchall()
    except sqlite3.Error as exc:
        raise DocsDatabaseError(f"Lecture du manifeste documentaire impossible : {exc}") from exc
    return {str(row["source_key"]): dict(row) for row in rows}


def list_docs_history(history_dir: Path | None = None) -> list[Path]:
    directory = (history_dir or settings.docs_history_dir).resolve()
    if not directory.exists():
        return []
    return sorted(
        (path for path in directory.glob("docs-*.db") if path.is_file()),
        reverse=True,
    )


def prune_docs_history(history_dir: Path | None = None, *, keep: int | None = None) -> list[Path]:
    directory = (history_dir or settings.docs_history_dir).resolve()
    keep_count = settings.docs_history_keep if keep is None else keep
    if keep_count < 0:
        raise DocsDatabaseError("La rétention documentaire doit être positive ou nulle.")
    history = list_docs_history(directory)
    if keep_count == 0:
        return []
    deleted: list[Path] = []
    for path in history[keep_count:]:
        path.unlink(missing_ok=True)
        deleted.append(path)
    if deleted:
        _fsync_directory(directory)
    return deleted


def activate_docs_database(
    candidate_path: Path,
    *,
    current_path: Path | None = None,
    history_dir: Path | None = None,
    keep_history: int | None = None,
) -> dict[str, str | None]:
    candidate = candidate_path.resolve()
    current = (current_path or settings.docs_db).resolve()
    history = (history_dir or settings.docs_history_dir).resolve()
    keep_count = settings.docs_history_keep if keep_history is None else keep_history

    if candidate == current:
        raise DocsDatabaseError("La base candidate doit être distincte de docs.db.")
    if keep_count < 0:
        raise DocsDatabaseError("La rétention documentaire doit être positive ou nulle.")

    validate_docs_database(candidate, require_delete_journal=True)
    current.parent.mkdir(parents=True, exist_ok=True)
    history.mkdir(parents=True, exist_ok=True)

    if os.stat(candidate).st_dev != os.stat(current.parent).st_dev:
        raise DocsDatabaseError("La base candidate doit être sur le même système de fichiers que docs.db.")
    if os.stat(history).st_dev != os.stat(current.parent).st_dev:
        raise DocsDatabaseError("L'historique docs.db doit être sur le même système de fichiers que docs.db.")

    archived_path: Path | None = None
    if current.exists():
        validate_docs_database(current, require_delete_journal=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        archived_path = history / f"docs-{stamp}-{_sha256_file(current)[:12]}.db"
        os.link(current, archived_path)
        _fsync_directory(history)

    os.replace(candidate, current)
    _fsync_directory(current.parent)
    validate_docs_database(current, require_delete_journal=True)
    prune_docs_history(history, keep=keep_count)

    return {
        "active": str(current),
        "archived": str(archived_path) if archived_path else None,
        "active_sha256": _sha256_file(current),
    }


def rollback_docs_database(
    archived_path: Path,
    *,
    current_path: Path | None = None,
    history_dir: Path | None = None,
    candidate_dir: Path | None = None,
    keep_history: int | None = None,
) -> dict[str, str | None]:
    source = archived_path.resolve()
    history = (history_dir or settings.docs_history_dir).resolve()
    candidates = (candidate_dir or settings.docs_candidates_dir).resolve()
    if source.parent != history or source not in list_docs_history(history):
        raise DocsDatabaseError(f"La base de rollback doit provenir de l'historique documentaire : {source}")
    validate_docs_database(source, require_delete_journal=True)

    candidates.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    staged = candidates / f"rollback-{stamp}-{_sha256_file(source)[:12]}.db"
    os.link(source, staged)
    _fsync_directory(candidates)
    try:
        result = activate_docs_database(
            staged,
            current_path=current_path,
            history_dir=history,
            keep_history=keep_history,
        )
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    return {**result, "rolled_back_from": str(source)}


def _reference_query(
    *,
    document_hash: str | None,
    version: str | None,
    revision_date: str | None,
    heading_path: str | None,
) -> tuple[str, list[str]]:
    clauses = ["s.source_url = ?"]
    values: list[str] = []
    if document_hash:
        clauses.append("d.content_hash = ?")
        values.append(document_hash)
    else:
        if version:
            clauses.append("d.detected_version = ?")
            values.append(version)
        if revision_date:
            clauses.append("d.detected_revision_date = ?")
            values.append(revision_date)
        if heading_path:
            clauses.append("s.heading_path = ?")
            values.append(heading_path)
    return " AND ".join(clauses), values


def find_section_reference(
    *,
    official_url: str,
    document_hash: str | None = None,
    version: str | None = None,
    revision_date: str | None = None,
    heading_path: str | None = None,
    current_path: Path | None = None,
    history_dir: Path | None = None,
) -> dict | None:
    current = (current_path or settings.docs_db).resolve()
    history = list_docs_history(history_dir)
    # Citations créées avant l'ajout de document_hash appartiennent au corpus
    # actif au moment de cette migration. Après la première mise à jour, ce corpus
    # devient donc la plus ancienne entrée de l'historique. Le parcourir du plus
    # ancien au plus récent évite de servir une révision ultérieure partageant la
    # même URL/version/chemin.
    paths = ([current] + history) if document_hash else (list(reversed(history)) + [current])
    where_sql, extra_values = _reference_query(
        document_hash=document_hash,
        version=version,
        revision_date=revision_date,
        heading_path=heading_path,
    )
    sql = f"""
        SELECT s.*, d.name AS document_name, d.detected_version, d.detected_revision_date,
               d.retrieved_at, d.content_hash
        FROM sections s
        JOIN documents d ON d.id = s.document_id
        WHERE {where_sql}
        ORDER BY d.source_priority DESC, s.section_order ASC
        LIMIT 1
    """

    for path in paths:
        if not path.is_file():
            continue
        try:
            with _readonly_connection(path) as conn:
                row = conn.execute(sql, [official_url, *extra_values]).fetchone()
        except sqlite3.Error:
            continue
        if row:
            result = dict(row)
            result["is_archived"] = path != current
            return result
    return None
