from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import etpos_assistant.docs_update as docs_update
from etpos_assistant.config import settings
from etpos_assistant.docs_store import DocsDatabaseError, document_manifest
from etpos_assistant.ingestion.service import prepare_source, registry_validation_benchmarks


SOURCE = {
    "id": "etpos-support-fr",
    "name": "Support ETPOS - Français",
    "url": "https://etpos.fr/fr/suporte",
    "type": "support",
    "priority": 60,
    "enabled": True,
    "validation_profile": "support_faq_answers",
    "min_faq_questions": 2,
    "min_answered_faqs": 2,
    "min_faq_answer_chars": 20,
}

PAGE_HTML = '''
<html><body><main><section id="faqs">
  <button>Question 1</button><button>Question 2</button>
</section></main>
<astro-island component-url="/_astro/AppBridge.TEST.js" component-export="PageIsland"></astro-island>
</body></html>
'''


def _bundle(second_answer: str = "La seconde réponse officielle est suffisamment détaillée pour être indexée.") -> str:
    return (
        'const x={fr:{faqs:['
        '{question:"1. Première question ?",answer:"La première réponse officielle est suffisamment détaillée pour être indexée."},'
        f'{{question:"2. Deuxième question ?",answer:"{second_answer}"}}'
        '],tutorials:te}};'
    )


@pytest.mark.asyncio
async def test_prepare_source_hash_includes_client_bundle_content():
    async def html_download(_url: str):
        return PAGE_HTML, "page-digest"

    async def bundle_v1(_url: str):
        return _bundle("Réponse version un suffisamment détaillée pour le test."), "bundle-digest-v1"

    async def bundle_v2(_url: str):
        return _bundle("Réponse version deux suffisamment détaillée pour le test."), "bundle-digest-v2"

    first = await prepare_source(
        SOURCE,
        html_downloader=html_download,
        javascript_downloader=bundle_v1,
    )
    second = await prepare_source(
        SOURCE,
        html_downloader=html_download,
        javascript_downloader=bundle_v2,
    )

    assert first.digest != second.digest
    assert first.snapshot_suffix == ".support.json"
    assert len(first.parsed.sections) == 2
    snapshot = json.loads(first.snapshot_text)
    assert snapshot["page_url"] == SOURCE["url"]
    assert snapshot["component_url"] == "https://etpos.fr/_astro/AppBridge.TEST.js"
    assert "component_javascript" in snapshot


@pytest.mark.asyncio
async def test_docs_candidate_can_index_client_side_support_without_touching_active_db(tmp_path, monkeypatch):
    registry = tmp_path / "sources.json"
    registry.write_text(json.dumps({"sources": [SOURCE]}), encoding="utf-8")
    current = tmp_path / "active" / "docs.db"
    candidates = tmp_path / "candidates"
    snapshots = tmp_path / "snapshots"

    async def html_download(_url: str):
        return PAGE_HTML, "page-digest"

    async def javascript_download(url: str):
        assert url == "https://etpos.fr/_astro/AppBridge.TEST.js"
        return _bundle(), "bundle-digest"

    monkeypatch.setattr(docs_update, "download_html", html_download)
    monkeypatch.setattr(docs_update, "download_javascript", javascript_download)
    previous_snapshot_dir = settings.snapshot_dir
    object.__setattr__(settings, "snapshot_dir", snapshots)
    try:
        built = await docs_update.build_docs_candidate(
            registry_path=registry,
            current_path=current,
            candidate_dir=candidates,
        )
    finally:
        object.__setattr__(settings, "snapshot_dir", previous_snapshot_dir)

    assert built.status == "candidate"
    assert built.candidate_path is not None and built.candidate_path.is_file()
    assert built.changed_sources == ("etpos-support-fr",)
    assert not current.exists()

    manifest = document_manifest(built.candidate_path)
    support = manifest["etpos-support-fr"]
    assert support["content_hash"]
    assert support["snapshot_path"].endswith(".support.json")
    assert Path(support["snapshot_path"]).is_file()

    with sqlite3.connect(built.candidate_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT title, heading_path, source_text FROM sections ORDER BY section_order"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["heading_path"].startswith("Support ETPOS > FAQ >")
    assert "première réponse officielle" in rows[0]["source_text"].lower()


@pytest.mark.asyncio
async def test_docs_candidate_can_include_disabled_source_only_for_explicit_candidate(tmp_path, monkeypatch):
    registry = tmp_path / "sources.json"
    registry.write_text(
        json.dumps({"sources": [{**SOURCE, "enabled": False}]}),
        encoding="utf-8",
    )
    current = tmp_path / "active" / "docs.db"
    candidates = tmp_path / "candidates"
    snapshots = tmp_path / "snapshots"

    async def html_download(_url: str):
        return PAGE_HTML, "page-digest"

    async def javascript_download(_url: str):
        return _bundle(), "bundle-digest"

    monkeypatch.setattr(docs_update, "download_html", html_download)
    monkeypatch.setattr(docs_update, "download_javascript", javascript_download)
    previous_snapshot_dir = settings.snapshot_dir
    object.__setattr__(settings, "snapshot_dir", snapshots)
    try:
        built = await docs_update.build_docs_candidate(
            registry_path=registry,
            current_path=current,
            candidate_dir=candidates,
            extra_source_ids=("etpos-support-fr",),
        )
    finally:
        object.__setattr__(settings, "snapshot_dir", previous_snapshot_dir)

    assert built.status == "candidate"
    assert built.changed_sources == ("etpos-support-fr",)
    assert built.candidate_path is not None
    assert "etpos-support-fr" in document_manifest(built.candidate_path)


@pytest.mark.asyncio
async def test_docs_candidate_rejects_unknown_extra_source(tmp_path):
    registry = tmp_path / "sources.json"
    registry.write_text(
        json.dumps({"sources": [{**SOURCE, "enabled": False}]}),
        encoding="utf-8",
    )

    with pytest.raises(DocsDatabaseError, match="Source candidate inconnue"):
        await docs_update.build_docs_candidate(
            registry_path=registry,
            current_path=tmp_path / "docs.db",
            candidate_dir=tmp_path / "candidates",
            extra_source_ids=("unknown-source",),
        )


def test_registry_validation_benchmarks_include_global_and_selected_source_contracts(tmp_path):
    registry = tmp_path / "sources.json"
    registry.write_text(
        json.dumps(
            {
                "validation_benchmarks": ["eval/acceptance.jsonl"],
                "sources": [
                    {"id": "manual", "enabled": True},
                    {
                        **SOURCE,
                        "enabled": False,
                        "validation_benchmarks": ["eval/support.jsonl"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    assert registry_validation_benchmarks(registry) == ("eval/acceptance.jsonl",)
    assert registry_validation_benchmarks(
        registry,
        extra_source_ids=("etpos-support-fr",),
    ) == ("eval/acceptance.jsonl", "eval/support.jsonl")


def test_registry_validation_benchmarks_reject_invalid_paths(tmp_path):
    registry = tmp_path / "sources.json"
    registry.write_text(
        json.dumps({"validation_benchmarks": [""], "sources": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="validation_benchmarks"):
        registry_validation_benchmarks(registry)
