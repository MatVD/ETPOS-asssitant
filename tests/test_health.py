from pathlib import Path
from types import SimpleNamespace

import etpos_assistant.db as db_module
from etpos_assistant.db import docs_db, init_all
from etpos_assistant.routers import health as health_module


def _settings(tmp_path: Path):
    data_dir = tmp_path / "data"
    return SimpleNamespace(
        data_dir=data_dir,
        app_db=data_dir / "app.db",
        docs_db=data_dir / "docs.db",
        ensure_dirs=lambda: data_dir.mkdir(parents=True, exist_ok=True),
    )


def test_ready_rejects_empty_corpus(monkeypatch, tmp_path):
    monkeypatch.setattr(db_module, "settings", _settings(tmp_path))
    init_all()

    response = health_module.ready()
    assert response.status_code == 503
    assert b'"corpus_empty"' in response.body


def test_ready_accepts_non_empty_corpus(monkeypatch, tmp_path):
    monkeypatch.setattr(db_module, "settings", _settings(tmp_path))
    init_all()

    with docs_db() as conn:
        document_id = conn.execute(
            """
            INSERT INTO documents(source_key,name,source_url,source_type,source_priority,language,retrieved_at,content_hash,snapshot_path)
            VALUES ('manual','Manuel','https://example.test','manual',100,'fr','2026-01-01','abc','snapshot')
            """
        ).lastrowid
        conn.execute(
            """
            INSERT INTO sections(document_id,section_order,title,heading_path,source_url,source_text,search_text)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                document_id,
                1,
                "Sauvegarde",
                "Système > Sauvegarde",
                "https://example.test#1",
                "Procédure de sauvegarde",
                "Sauvegarde Système Procédure de sauvegarde",
            ),
        )

    response = health_module.ready()
    assert response.status_code == 200
    assert b'"status":"ok"' in response.body
