from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import settings
from .db import init_docs_db
from .docs_store import (
    DocsDatabaseError,
    build_versions_match,
    document_manifest,
    validate_docs_database,
    write_build_metadata,
)
from .ingestion.fetch import download_html, save_snapshot
from .ingestion.parser import parse_html
from .ingestion.service import index_parsed_source, load_registry


@dataclass(frozen=True)
class DownloadedSource:
    source: dict
    html: str
    digest: str


@dataclass(frozen=True)
class DocsUpdateCandidate:
    status: str
    candidate_path: Path | None
    changed_sources: tuple[str, ...]
    removed_sources: tuple[str, ...]
    results: tuple[dict, ...]
    corpus_fingerprint: str

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "candidate_path": str(self.candidate_path) if self.candidate_path else None,
            "changed_sources": list(self.changed_sources),
            "removed_sources": list(self.removed_sources),
            "results": list(self.results),
            "corpus_fingerprint": self.corpus_fingerprint,
        }


def _corpus_fingerprint(downloaded: list[DownloadedSource]) -> str:
    digest = hashlib.sha256()
    for item in sorted(downloaded, key=lambda entry: str(entry.source["id"])):
        digest.update(str(item.source["id"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.digest.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _remove_sqlite_files(path: Path) -> None:
    path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm", "-journal"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


async def build_docs_candidate(
    *,
    registry_path: Path = Path("config/sources.json"),
    current_path: Path | None = None,
    candidate_dir: Path | None = None,
) -> DocsUpdateCandidate:
    sources = load_registry(registry_path)
    if not sources:
        raise DocsDatabaseError("Aucune source documentaire activée dans le registre.")

    current = current_path or settings.docs_db
    manifest = document_manifest(current)
    downloaded: list[DownloadedSource] = []
    for source in sources:
        html, digest = await download_html(source["url"])
        downloaded.append(DownloadedSource(source=source, html=html, digest=digest))

    fingerprint = _corpus_fingerprint(downloaded)
    enabled_ids = {str(item.source["id"]) for item in downloaded}
    current_ids = set(manifest)
    removed_sources = tuple(sorted(current_ids - enabled_ids))
    changed_sources = tuple(
        sorted(
            str(item.source["id"])
            for item in downloaded
            if item.source["id"] not in manifest
            or manifest[item.source["id"]]["content_hash"] != item.digest
        )
    )

    versions_match = build_versions_match(current)

    if not changed_sources and not removed_sources and current.is_file() and versions_match:
        return DocsUpdateCandidate(
            status="unchanged",
            candidate_path=None,
            changed_sources=(),
            removed_sources=(),
            results=(),
            corpus_fingerprint=fingerprint,
        )

    candidates = (candidate_dir or settings.docs_candidates_dir).resolve()
    candidates.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = candidates / f"docs-{stamp}-{fingerprint[:12]}.db"
    if candidate.exists():
        raise DocsDatabaseError(f"La base candidate existe déjà : {candidate}")

    init_docs_db(candidate)
    results: list[dict] = []
    retrieved_at = datetime.now(UTC).isoformat()
    try:
        for item in downloaded:
            source_id = str(item.source["id"])
            existing = manifest.get(source_id)
            existing_snapshot = Path(str(existing.get("snapshot_path", ""))) if existing else None
            if (
                existing
                and existing.get("content_hash") == item.digest
                and existing_snapshot
                and existing_snapshot.is_file()
            ):
                snapshot_path = str(existing_snapshot)
            else:
                snapshot_path = save_snapshot(source_id, item.html, item.digest)

            parsed = parse_html(item.html, item.source["url"])
            if not parsed.sections:
                raise DocsDatabaseError(f"Le parsing de {source_id} ne produit aucune section.")
            results.append(
                index_parsed_source(
                    item.source,
                    parsed,
                    item.digest,
                    snapshot_path,
                    retrieved_at,
                    db_path=candidate,
                    skip_if_unchanged=False,
                )
            )
        write_build_metadata(candidate)
        validate_docs_database(candidate, require_delete_journal=True)
    except Exception:
        _remove_sqlite_files(candidate)
        raise

    return DocsUpdateCandidate(
        status="candidate",
        candidate_path=candidate,
        changed_sources=changed_sources,
        removed_sources=removed_sources,
        results=tuple(results),
        corpus_fingerprint=fingerprint,
    )
