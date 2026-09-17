from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import etpos_assistant.docs_update as docs_update
import etpos_assistant.ingestion.service as ingestion_service
from etpos_assistant.citations import normalize_citation_link
from etpos_assistant.config import settings
from etpos_assistant.db import init_docs_db
from etpos_assistant.docs_store import (
    activate_docs_database,
    document_manifest,
    find_section_reference,
    list_docs_history,
    prune_docs_history,
    read_build_metadata,
    rollback_docs_database,
    validate_docs_database,
    write_build_metadata,
)
from etpos_assistant.docs_versions import INDEX_VERSION, PARSER_VERSION
from etpos_assistant.ingestion.parser import parse_html
from etpos_assistant.ingestion.service import index_parsed_source


SOURCE = {
    "id": "etpos-user-manual-fr",
    "name": "Manuel utilisateur ETPOS - Français",
    "url": "https://www.etcloud.pt/manual.html",
    "type": "manual",
    "priority": 100,
    "enabled": True,
}


def _digest(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def _build_database(path: Path, html: str, snapshot_path: Path) -> tuple[str, str]:
    digest = _digest(html)
    parsed = parse_html(html, SOURCE["url"])
    init_docs_db(path)
    index_parsed_source(
        SOURCE,
        parsed,
        digest,
        str(snapshot_path),
        datetime.now(UTC).isoformat(),
        db_path=path,
        skip_if_unchanged=False,
    )
    write_build_metadata(path)
    backup = next(section for section in parsed.sections if section.title == "Sauvegarde")
    return digest, backup.source_url


def test_atomic_activation_keeps_history_and_resolves_old_citations(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "sample_manual.html"
    html_v1 = fixture.read_text(encoding="utf-8")
    html_v2 = html_v1.replace(
        "procédure de sauvegarde de démonstration",
        "procédure de sauvegarde de version deux",
    )
    snapshot_v1 = tmp_path / "snapshot-v1.html"
    snapshot_v2 = tmp_path / "snapshot-v2.html"
    snapshot_v1.write_text(html_v1, encoding="utf-8")
    snapshot_v2.write_text(html_v2, encoding="utf-8")

    current = tmp_path / "data" / "docs.db"
    candidate = tmp_path / "data" / "docs-candidates" / "candidate.db"
    history = tmp_path / "data" / "docs-history"
    hash_v1, backup_url = _build_database(current, html_v1, snapshot_v1)
    hash_v2, _ = _build_database(candidate, html_v2, snapshot_v2)

    assert validate_docs_database(current)["journal_mode"] == "delete"
    assert validate_docs_database(candidate)["journal_mode"] == "delete"

    activation = activate_docs_database(
        candidate,
        current_path=current,
        history_dir=history,
        keep_history=3,
    )

    assert activation["archived"]
    assert document_manifest(current)[SOURCE["id"]]["content_hash"] == hash_v2
    archived = list_docs_history(history)
    assert len(archived) == 1
    assert document_manifest(archived[0])[SOURCE["id"]]["content_hash"] == hash_v1

    old_section = find_section_reference(
        official_url=backup_url,
        document_hash=hash_v1,
        current_path=current,
        history_dir=history,
    )
    new_section = find_section_reference(
        official_url=backup_url,
        document_hash=hash_v2,
        current_path=current,
        history_dir=history,
    )
    legacy_section = find_section_reference(
        official_url=backup_url,
        current_path=current,
        history_dir=history,
    )

    assert old_section is not None and old_section["is_archived"] is True
    assert "démonstration" in old_section["source_text"]
    assert new_section is not None and new_section["is_archived"] is False
    assert "version deux" in new_section["source_text"]
    assert legacy_section is not None and legacy_section["is_archived"] is True

    rollback = rollback_docs_database(
        archived[0],
        current_path=current,
        history_dir=history,
        candidate_dir=tmp_path / "data" / "docs-candidates",
        keep_history=3,
    )
    assert rollback["rolled_back_from"] == str(archived[0])
    assert document_manifest(current)[SOURCE["id"]]["content_hash"] == hash_v1
    assert any(
        document_manifest(path)[SOURCE["id"]]["content_hash"] == hash_v2
        for path in list_docs_history(history)
    )


def test_legacy_citation_prefers_oldest_archived_matching_corpus(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "sample_manual.html"
    html_v1 = fixture.read_text(encoding="utf-8")
    html_v2 = html_v1.replace(
        "procédure de sauvegarde de démonstration",
        "procédure de sauvegarde de version deux",
    )
    html_v3 = html_v1.replace(
        "procédure de sauvegarde de démonstration",
        "procédure de sauvegarde de version trois",
    )
    current = tmp_path / "data" / "docs.db"
    history = tmp_path / "data" / "docs-history"
    candidate_dir = tmp_path / "data" / "docs-candidates"
    candidate_v2 = candidate_dir / "v2.db"
    candidate_v3 = candidate_dir / "v3.db"
    snapshot_v1 = tmp_path / "snapshot-v1.html"
    snapshot_v2 = tmp_path / "snapshot-v2.html"
    snapshot_v3 = tmp_path / "snapshot-v3.html"
    snapshot_v1.write_text(html_v1, encoding="utf-8")
    snapshot_v2.write_text(html_v2, encoding="utf-8")
    snapshot_v3.write_text(html_v3, encoding="utf-8")

    _hash_v1, backup_url = _build_database(current, html_v1, snapshot_v1)
    _build_database(candidate_v2, html_v2, snapshot_v2)
    activate_docs_database(candidate_v2, current_path=current, history_dir=history, keep_history=0)
    _build_database(candidate_v3, html_v3, snapshot_v3)
    activate_docs_database(candidate_v3, current_path=current, history_dir=history, keep_history=0)

    archived = list_docs_history(history)
    assert len(archived) == 2
    legacy_section = find_section_reference(
        official_url=backup_url,
        current_path=current,
        history_dir=history,
    )

    assert legacy_section is not None and legacy_section["is_archived"] is True
    assert "démonstration" in legacy_section["source_text"]


def test_init_docs_db_converts_legacy_wal_database_to_delete(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "sample_manual.html"
    html = fixture.read_text(encoding="utf-8")
    snapshot = tmp_path / "snapshot.html"
    snapshot.write_text(html, encoding="utf-8")
    database = tmp_path / "docs.db"
    _build_database(database, html, snapshot)

    conn = sqlite3.connect(database)
    try:
        assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() == "wal"
    finally:
        conn.close()
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()

    init_docs_db(database)

    assert validate_docs_database(database)["journal_mode"] == "delete"


def test_citation_link_is_rebuilt_from_stable_metadata():
    citation = normalize_citation_link(
        {
            "source_id": "S1",
            "official_url": "https://www.etcloud.pt/manual.html#sauvegarde",
            "document_hash": "abc123",
            "version": "V5.34",
            "heading_path": "SÉCURITÉ ET FIABILITÉ > Sauvegarde",
            "internal_url": "/sources/42",
        }
    )

    assert citation["internal_url"].startswith("/sources/view?")
    assert "hash=abc123" in citation["internal_url"]
    assert "%23sauvegarde" in citation["internal_url"]


@pytest.mark.asyncio
async def test_candidate_builder_detects_unchanged_then_builds_without_touching_current(
    tmp_path,
    monkeypatch,
):
    fixture = Path(__file__).parent / "fixtures" / "sample_manual.html"
    html_v1 = fixture.read_text(encoding="utf-8")
    html_v2 = html_v1.replace(
        "procédure de sauvegarde de démonstration",
        "procédure de sauvegarde candidate",
    )
    snapshot = tmp_path / "snapshots" / "current.html"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(html_v1, encoding="utf-8")
    current = tmp_path / "data" / "docs.db"
    hash_v1, _ = _build_database(current, html_v1, snapshot)

    registry = tmp_path / "sources.json"
    registry.write_text(json.dumps({"sources": [SOURCE]}, ensure_ascii=False), encoding="utf-8")
    candidates = tmp_path / "data" / "docs-candidates"

    async def same_download(_url: str):
        return html_v1, hash_v1

    monkeypatch.setattr(docs_update, "download_html", same_download)
    unchanged = await docs_update.build_docs_candidate(
        registry_path=registry,
        current_path=current,
        candidate_dir=candidates,
    )
    assert unchanged.status == "unchanged"
    assert unchanged.candidate_path is None

    hash_v2 = _digest(html_v2)

    async def changed_download(_url: str):
        return html_v2, hash_v2

    previous_snapshot_dir = settings.snapshot_dir
    object.__setattr__(settings, "snapshot_dir", tmp_path / "new-snapshots")
    monkeypatch.setattr(docs_update, "download_html", changed_download)
    try:
        built = await docs_update.build_docs_candidate(
            registry_path=registry,
            current_path=current,
            candidate_dir=candidates,
        )
    finally:
        object.__setattr__(settings, "snapshot_dir", previous_snapshot_dir)

    assert built.status == "candidate"
    assert built.candidate_path is not None
    assert built.changed_sources == (SOURCE["id"],)
    assert validate_docs_database(built.candidate_path)["sections"] >= 2
    assert document_manifest(built.candidate_path)[SOURCE["id"]]["content_hash"] == hash_v2
    assert document_manifest(current)[SOURCE["id"]]["content_hash"] == hash_v1


@pytest.mark.parametrize(
    ("metadata_column", "old_version"),
    (("parser_version", PARSER_VERSION - 1), ("index_version", INDEX_VERSION - 1)),
)
@pytest.mark.asyncio
async def test_candidate_builder_rebuilds_when_parser_or_index_version_changes(
    tmp_path, monkeypatch, metadata_column, old_version
):
    fixture = Path(__file__).parent / "fixtures" / "sample_manual.html"
    html = fixture.read_text(encoding="utf-8")
    snapshot = tmp_path / "snapshots" / "current.html"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(html, encoding="utf-8")
    current = tmp_path / "data" / "docs.db"
    digest, _ = _build_database(current, html, snapshot)

    registry = tmp_path / "sources.json"
    registry.write_text(json.dumps({"sources": [SOURCE]}, ensure_ascii=False), encoding="utf-8")
    candidates = tmp_path / "data" / "docs-candidates"

    with sqlite3.connect(current) as conn:
        conn.execute(f"UPDATE build_metadata SET {metadata_column} = ? WHERE id = 1", (old_version,))
        conn.commit()

    async def same_download(_url: str):
        return html, digest

    monkeypatch.setattr(docs_update, "download_html", same_download)
    rebuilt = await docs_update.build_docs_candidate(
        registry_path=registry,
        current_path=current,
        candidate_dir=candidates,
    )

    assert rebuilt.status == "candidate"
    assert rebuilt.changed_sources == ()
    assert rebuilt.candidate_path is not None
    metadata = read_build_metadata(rebuilt.candidate_path)
    assert metadata is not None
    assert metadata["parser_version"] == PARSER_VERSION
    assert metadata["index_version"] == INDEX_VERSION
    assert metadata["built_at"]


@pytest.mark.asyncio
async def test_dev_ingest_reindexes_when_build_version_changes(tmp_path, monkeypatch):
    fixture = Path(__file__).parent / "fixtures" / "sample_manual.html"
    html = fixture.read_text(encoding="utf-8")
    snapshot = tmp_path / "snapshot.html"
    snapshot.write_text(html, encoding="utf-8")
    database = tmp_path / "docs.db"
    digest, _ = _build_database(database, html, snapshot)

    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE build_metadata SET parser_version = ? WHERE id = 1", (PARSER_VERSION - 1,))
        conn.commit()

    async def same_download(_url: str):
        return html, digest

    monkeypatch.setattr(ingestion_service, "download_html", same_download)
    monkeypatch.setattr(
        ingestion_service,
        "save_snapshot",
        lambda _source_id, _text, _digest, suffix=".html": str(snapshot),
    )
    monkeypatch.setattr(ingestion_service, "load_registry", lambda: [SOURCE])

    results = await ingestion_service.ingest_enabled_sources(db_path=database)

    assert results[0]["status"] == "indexed"
    metadata = read_build_metadata(database)
    assert metadata is not None
    assert metadata["parser_version"] == PARSER_VERSION
    assert metadata["index_version"] == INDEX_VERSION


def test_prune_docs_history_with_zero_keep_preserves_every_archived_database(tmp_path):
    history = tmp_path / "docs-history"
    history.mkdir()
    first = history / "docs-20260917T080000000000Z-first.db"
    second = history / "docs-20260917T090000000000Z-second.db"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    deleted = prune_docs_history(history, keep=0)

    assert deleted == []
    assert list_docs_history(history) == [second, first]
