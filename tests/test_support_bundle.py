from __future__ import annotations

import pytest

from etpos_assistant.ingestion.support import (
    ensure_same_origin_asset,
    extract_french_faq_items,
    faq_items_to_document,
    find_support_component_url,
    validate_faq_items,
)
from etpos_assistant.ingestion.validation import SourceContentError


SOURCE = {
    "id": "etpos-support-fr",
    "min_faq_questions": 2,
    "min_answered_faqs": 2,
    "min_faq_answer_chars": 20,
}


def _bundle() -> str:
    return r'''const x={pt:{faqs:[{question:"PT",answer:"Resposta longa portuguesa"}]},fr:{faqs:[{question:"1. ETPOS est-il certifié ?",answer:`Oui. ETPOS est un logiciel certifié.
Il respecte les exigences applicables.`},{question:"2. Puis-je faire des sauvegardes ?",answer:"Oui. Vous pouvez sauvegarder les données sur une clé USB ou dans le cloud via ETCLOUD."}],tutorials:te},en:{faqs:[]}};'''


def test_find_support_component_url_prefers_page_island_appbridge():
    html = '''
    <html><body>
      <astro-island component-url="/_astro/Other.js" component-export="Other"></astro-island>
      <astro-island component-url="/_astro/AppBridge.ABC123.js" component-export="PageIsland"></astro-island>
    </body></html>
    '''

    assert find_support_component_url(html, "https://etpos.fr/fr/suporte") == (
        "https://etpos.fr/_astro/AppBridge.ABC123.js"
    )


def test_extract_french_faq_items_without_executing_javascript():
    items = extract_french_faq_items(_bundle())

    assert len(items) == 2
    assert items[0].question == "1. ETPOS est-il certifié ?"
    assert "logiciel certifié" in items[0].answer
    assert "clé USB" in items[1].answer


def test_extract_french_faq_rejects_template_interpolation():
    bundle = 'const x={fr:{faqs:[{question:"Q",answer:`Réponse ${dangerous()}`}],tutorials:te}};'

    with pytest.raises(SourceContentError, match="Interpolation JavaScript"):
        extract_french_faq_items(bundle)


def test_support_items_are_validated_and_mapped_to_document_sections():
    items = extract_french_faq_items(_bundle())
    validate_faq_items(SOURCE, items)
    document = faq_items_to_document(items, "https://etpos.fr/fr/suporte")

    assert document.title == "Support ETPOS - FAQ - Français"
    assert len(document.sections) == 2
    assert document.sections[0].heading_path.startswith("Support ETPOS > FAQ >")
    assert document.sections[0].source_url == "https://etpos.fr/fr/suporte#faqs"
    assert "ETPOS est-il certifié" in document.sections[0].search_text


def test_support_bundle_must_stay_on_same_official_origin():
    ensure_same_origin_asset(
        "https://etpos.fr/fr/suporte",
        "https://etpos.fr/_astro/AppBridge.ABC123.js",
    )

    with pytest.raises(SourceContentError, match="même hôte officiel"):
        ensure_same_origin_asset(
            "https://etpos.fr/fr/suporte",
            "https://cdn.example.com/AppBridge.js",
        )
