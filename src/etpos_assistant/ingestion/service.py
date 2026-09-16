from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ..db import docs_db, init_docs_db
from .fetch import fetch_html
from .parser import parse_html


def load_registry(path: Path = Path("config/sources.json")) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [source for source in data.get("sources", []) if source.get("enabled", False)]


async def ingest_source(source: dict) -> dict:
    html, digest, snapshot_path = await fetch_html(source["id"], source["url"])
    parsed = parse_html(html, source["url"])
    retrieved_at = datetime.now(UTC).isoformat()

    with docs_db() as conn:
        existing = conn.execute(
            "SELECT id, content_hash FROM documents WHERE source_key = ?", (source["id"],)
        ).fetchone()
        if existing and existing["content_hash"] == digest:
            return {"source": source["id"], "status": "unchanged", "sections": 0}

        conn.execute(
            """
            INSERT INTO documents(
                source_key, name, source_url, source_type, source_priority, language,
                detected_version, detected_revision_date, retrieved_at, content_hash, snapshot_path
            ) VALUES (?, ?, ?, ?, ?, 'fr', ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                name=excluded.name,
                source_url=excluded.source_url,
                source_type=excluded.source_type,
                source_priority=excluded.source_priority,
                detected_version=excluded.detected_version,
                detected_revision_date=excluded.detected_revision_date,
                retrieved_at=excluded.retrieved_at,
                content_hash=excluded.content_hash,
                snapshot_path=excluded.snapshot_path
            """,
            (
                source["id"], source["name"], source["url"], source.get("type", "manual"),
                int(source.get("priority", 0)), parsed.version, parsed.revision_date,
                retrieved_at, digest, snapshot_path,
            ),
        )
        document_id = conn.execute(
            "SELECT id FROM documents WHERE source_key = ?", (source["id"],)
        ).fetchone()["id"]
        conn.execute("DELETE FROM sections WHERE document_id = ?", (document_id,))
        for section in parsed.sections:
            conn.execute(
                """
                INSERT INTO sections(
                    document_id, section_order, title, heading_path, anchor, source_url,
                    source_text, search_text, image_refs_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id, section.order, section.title, section.heading_path, section.anchor,
                    section.source_url, section.source_text, section.search_text,
                    json.dumps(section.image_refs, ensure_ascii=False),
                ),
            )
    return {
        "source": source["id"],
        "status": "indexed",
        "sections": len(parsed.sections),
        "version": parsed.version,
        "revision_date": parsed.revision_date,
    }


async def ingest_enabled_sources() -> list[dict]:
    init_docs_db()
    results = []
    for source in load_registry():
        results.append(await ingest_source(source))
    return results
