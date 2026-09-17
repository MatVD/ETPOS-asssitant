from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import etpos_assistant.cli as cli
import etpos_assistant.ingestion.fetch as fetch_module
import etpos_assistant.ingestion.service as service
from etpos_assistant.ingestion.fetch import validate_source_url
from etpos_assistant.ingestion.validation import SourceContentError, validate_source_html


SUPPORT_SOURCE = {
    "id": "etpos-support-fr",
    "name": "Support ETPOS - Français",
    "url": "https://etpos.fr/fr/suporte",
    "type": "support",
    "priority": 60,
    "validation_profile": "support_faq_answers",
    "min_faq_questions": 2,
    "min_answered_faqs": 2,
    "min_faq_answer_chars": 20,
}


def _support_html(*, with_answers: bool) -> str:
    answer_1 = "<div><p>ETPOS permet de configurer des sauvegardes selon les options documentées.</p></div>" if with_answers else ""
    answer_2 = "<div><p>Chaque collaborateur peut disposer de permissions adaptées à son profil.</p></div>" if with_answers else ""
    return f"""
    <html><body><main>
      <section id="faqs">
        <h2>FAQs</h2>
        <div><button><p>1. Est-il possible d'effectuer des sauvegardes ?</p></button>{answer_1}</div>
        <div><button><p>2. Chaque collaborateur peut-il avoir un profil différent ?</p></button>{answer_2}</div>
      </section>
    </main></body></html>
    """


def test_support_validation_rejects_question_only_faq():
    with pytest.raises(SourceContentError, match=r"réponse\(s\) FAQ exploitable"):
        validate_source_html(SUPPORT_SOURCE, _support_html(with_answers=False))


def test_support_validation_accepts_faq_with_real_answers():
    validate_source_html(SUPPORT_SOURCE, _support_html(with_answers=True))


@pytest.mark.asyncio
async def test_ingest_source_fails_closed_before_indexing_question_only_support(monkeypatch):
    async def fake_download_html(url: str):
        return _support_html(with_answers=False), "digest"

    monkeypatch.setattr(service, "download_html", fake_download_html)

    with pytest.raises(SourceContentError):
        await service.ingest_source(SUPPORT_SOURCE)


def test_support_registry_is_enabled_only_with_quality_gates_declared():
    registry_path = Path(__file__).resolve().parents[1] / "config" / "sources.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    support = next(source for source in registry["sources"] if source["id"] == "etpos-support-fr")

    assert support["enabled"] is True
    assert support["validation_profile"] == "support_faq_answers"
    assert support["validation_benchmarks"] == ["eval/support.jsonl"]
    assert "eval/acceptance.jsonl" in registry["validation_benchmarks"]
    assert support["min_answered_faqs"] >= 3
    assert support["url"].startswith("https://etpos.fr/")


def test_source_url_allowlist_includes_official_french_domain_only():
    validate_source_url("https://etpos.fr/fr/suporte")
    validate_source_url("https://www.etcloud.pt/downloads/manual.html")

    with pytest.raises(ValueError, match="Source non autorisée"):
        validate_source_url("https://example.com/fake-etpos")
    with pytest.raises(ValueError, match="Source non autorisée"):
        validate_source_url("https://etpos.fr:444/fr/suporte")


@pytest.mark.asyncio
async def test_download_refuses_external_redirect_before_following(monkeypatch):
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if len(requested_urls) > 1:
            pytest.fail("La redirection externe ne doit jamais être requêtée")
        return httpx.Response(
            302,
            headers={"location": "https://example.com/redirected"},
            request=request,
        )

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        fetch_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )

    with pytest.raises(ValueError, match="Source non autorisée"):
        await fetch_module.download_html("https://etpos.fr/fr/suporte")

    assert requested_urls == ["https://etpos.fr/fr/suporte"]


@pytest.mark.asyncio
async def test_inspect_source_uses_static_client_bundle_when_faq_answers_are_client_side(
    tmp_path, monkeypatch, capsys
):
    registry = tmp_path / "sources.json"
    registry.write_text(json.dumps({"sources": [{**SUPPORT_SOURCE, "enabled": False}]}), encoding="utf-8")
    page_html = '''
    <html><body><main><section id="faqs">
      <button>Question 1</button><button>Question 2</button>
    </section></main>
    <astro-island component-url="/_astro/AppBridge.TEST.js" component-export="PageIsland"></astro-island>
    </body></html>
    '''
    bundle = r'''const x={fr:{faqs:[{question:"1. Question",answer:"Une réponse officielle suffisamment longue pour être indexée."},{question:"2. Question",answer:"Une deuxième réponse officielle suffisamment longue pour être indexée."}],tutorials:te}};'''

    async def fake_download_html(url: str):
        return page_html, "page-digest"

    async def fake_download_javascript(url: str):
        assert url == "https://etpos.fr/_astro/AppBridge.TEST.js"
        return bundle, "bundle-digest"

    monkeypatch.setattr(cli, "download_html", fake_download_html)
    monkeypatch.setattr(cli, "download_javascript", fake_download_javascript)
    args = SimpleNamespace(registry=str(registry), source_id="etpos-support-fr")

    await cli._run_inspect_source(args)

    report = json.loads(capsys.readouterr().out)
    assert report["validation"] == "ok"
    assert report["validation_method"] == "static_client_bundle"
    assert report["extracted_faqs"] == 2
    assert report["component_digest"] == "bundle-digest"
    assert report["sections"] == 2


@pytest.mark.asyncio
async def test_inspect_source_reports_quality_failure_without_indexing(tmp_path, monkeypatch, capsys):
    registry = tmp_path / "sources.json"
    registry.write_text(json.dumps({"sources": [{**SUPPORT_SOURCE, "enabled": False}]}), encoding="utf-8")

    async def fake_download_html(url: str):
        return _support_html(with_answers=False), "digest-123"

    monkeypatch.setattr(cli, "download_html", fake_download_html)
    args = SimpleNamespace(registry=str(registry), source_id="etpos-support-fr")

    with pytest.raises(SystemExit, match="refusée"):
        await cli._run_inspect_source(args)

    report = json.loads(capsys.readouterr().out)
    assert report["enabled"] is False
    assert report["validation"] == "failed"
    assert report["sections"] >= 1
    assert "réponse(s) FAQ exploitable" in report["validation_error"]
