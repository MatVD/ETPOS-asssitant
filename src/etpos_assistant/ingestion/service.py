from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Awaitable, Callable

from ..config import settings
from ..db import docs_db, init_docs_db
from ..docs_store import build_versions_match, write_build_metadata
from .fetch import download_html, download_javascript, save_snapshot
from .parser import ParsedDocument, parse_html
from .support import (
    composite_support_digest,
    ensure_same_origin_asset,
    find_support_component_url,
    parse_support_client_bundle,
    support_snapshot_payload,
)
from .validation import SourceContentError, validate_source_html


def load_registry_config(path: Path = Path("config/sources.json")) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: le registre de sources doit être un objet JSON.")
    return data


def load_registry(
    path: Path = Path("config/sources.json"),
    *,
    enabled_only: bool = True,
) -> list[dict]:
    data = load_registry_config(path)
    sources = list(data.get("sources", []))
    if enabled_only:
        return [source for source in sources if source.get("enabled", False)]
    return sources


def registry_validation_benchmarks(
    path: Path = Path("config/sources.json"),
    *,
    extra_source_ids: tuple[str, ...] = (),
) -> tuple[str, ...]:
    data = load_registry_config(path)
    sources = list(data.get("sources", []))
    selected_ids = {
        str(source.get("id"))
        for source in sources
        if source.get("enabled", False)
    }
    selected_ids.update(source_id for source_id in extra_source_ids if source_id)

    configured: list[object] = list(data.get("validation_benchmarks", []))
    for source in sources:
        if str(source.get("id")) in selected_ids:
            configured.extend(source.get("validation_benchmarks", []))

    paths: list[str] = []
    for value in configured:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path}: validation_benchmarks doit contenir uniquement des chemins non vides.")
        normalized = value.strip()
        if normalized not in paths:
            paths.append(normalized)
    return tuple(paths)


@dataclass(frozen=True)
class PreparedSource:
    parsed: ParsedDocument
    digest: str
    snapshot_text: str
    snapshot_suffix: str
    component_url: str | None = None
    component_digest: str | None = None


TextDownloader = Callable[[str], Awaitable[tuple[str, str]]]


async def prepare_source(
    source: dict,
    *,
    html_downloader: TextDownloader | None = None,
    javascript_downloader: TextDownloader | None = None,
) -> PreparedSource:
    html_fetch = html_downloader or download_html
    javascript_fetch = javascript_downloader or download_javascript
    html, page_digest = await html_fetch(source["url"])
    try:
        validate_source_html(source, html)
        return PreparedSource(
            parsed=parse_html(html, source["url"]),
            digest=page_digest,
            snapshot_text=html,
            snapshot_suffix=".html",
        )
    except SourceContentError:
        if str(source.get("validation_profile") or "").strip().lower() != "support_faq_answers":
            raise

    component_url = find_support_component_url(html, source["url"])
    ensure_same_origin_asset(source["url"], component_url)
    bundle_text, bundle_digest = await javascript_fetch(component_url)
    parsed, parsed_component_url, _faq_count = parse_support_client_bundle(source, html, bundle_text)
    if parsed_component_url != component_url:
        raise SourceContentError("Le bundle Support analysé ne correspond pas au composant téléchargé.")
    digest = composite_support_digest(page_digest, bundle_digest, component_url)
    return PreparedSource(
        parsed=parsed,
        digest=digest,
        snapshot_text=support_snapshot_payload(source["url"], html, component_url, bundle_text),
        snapshot_suffix=".support.json",
        component_url=component_url,
        component_digest=bundle_digest,
    )


def index_parsed_source(
    source: dict,
    parsed: ParsedDocument,
    digest: str,
    snapshot_path: str,
    retrieved_at: str,
    *,
    db_path: Path | None = None,
    skip_if_unchanged: bool = True,
) -> dict:
    with docs_db(db_path) as conn:
        existing = conn.execute(
            "SELECT id, content_hash FROM documents WHERE source_key = ?", (source["id"],)
        ).fetchone()
        if skip_if_unchanged and existing and existing["content_hash"] == digest:
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


async def ingest_source(
    source: dict,
    *,
    db_path: Path | None = None,
    skip_if_unchanged: bool = True,
) -> dict:
    prepared = await prepare_source(source)
    snapshot_path = save_snapshot(
        source["id"],
        prepared.snapshot_text,
        prepared.digest,
        suffix=prepared.snapshot_suffix,
    )
    return index_parsed_source(
        source,
        prepared.parsed,
        prepared.digest,
        snapshot_path,
        datetime.now(UTC).isoformat(),
        db_path=db_path,
        skip_if_unchanged=skip_if_unchanged,
    )


async def ingest_enabled_sources(*, db_path: Path | None = None) -> list[dict]:
    init_docs_db(db_path)
    versions_match = build_versions_match(db_path)
    results = []
    for source in load_registry():
        results.append(
            await ingest_source(
                source,
                db_path=db_path,
                skip_if_unchanged=versions_match,
            )
        )
    write_build_metadata(db_path or settings.docs_db)
    return results
