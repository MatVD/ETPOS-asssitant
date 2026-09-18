from pathlib import Path

from etpos_assistant.ingestion.parser import parse_html


def test_parser_extracts_sections_and_metadata():
    html = (Path(__file__).parent / "fixtures" / "sample_manual.html").read_text(encoding="utf-8")
    parsed = parse_html(html, "https://www.etcloud.pt/manual.html")
    assert parsed.version == "V5.34"
    assert parsed.revision_date == "2026-01-30"
    assert len(parsed.sections) >= 2
    assert all("Table des matières" not in section.source_text for section in parsed.sections)
    backup = next(section for section in parsed.sections if section.title == "Sauvegarde")
    assert "Système > Sauvegarde" in backup.heading_path
    assert "procédure de sauvegarde" in backup.source_text
    assert backup.source_url.endswith("#sauvegarde")
    assert backup.image_refs[0]["url"] == "https://www.etcloud.pt/images/backup.png"
    assert "ignore_me" not in " ".join(section.source_text for section in parsed.sections)


def test_parser_uses_article_published_time_when_revision_is_absent():
    html = """
    <html><head>
      <title>Actualité ETPOS</title>
      <meta property="article:published_time" content="2026-08-04T23:00:00.000Z">
    </head><body><article>
      <h1>Actualité ETPOS</h1>
      <p>Un contenu officiel suffisamment long pour constituer une section documentaire exploitable dans le parser.</p>
    </article></body></html>
    """
    parsed = parse_html(html, "https://etpos.fr/fr/blog/noticia/test")
    assert parsed.revision_date == "2026-08-04"
