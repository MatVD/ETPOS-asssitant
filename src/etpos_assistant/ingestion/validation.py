from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag


class SourceContentError(ValueError):
    """Raised when a declared source does not expose enough trustworthy content."""


def _answer_text_for_button(soup: BeautifulSoup, button: Tag) -> str:
    controlled_id = button.get("aria-controls")
    if controlled_id:
        target = soup.find(id=controlled_id)
        if isinstance(target, Tag):
            return target.get_text(" ", strip=True)

    parent = button.parent
    if not isinstance(parent, Tag):
        return ""

    fragments: list[str] = []
    seen_button = False
    for child in parent.children:
        if child is button:
            seen_button = True
            continue
        if not seen_button:
            continue
        if isinstance(child, Tag):
            fragments.append(child.get_text(" ", strip=True))
    return " ".join(fragment for fragment in fragments if fragment).strip()


def _validate_support_faq_answers(source: dict, html: str) -> None:
    soup = BeautifulSoup(html, "html.parser")
    faq_root = soup.select_one("#faqs")
    if not isinstance(faq_root, Tag):
        raise SourceContentError(
            f"{source['id']}: la section FAQ attendue (#faqs) est absente de la source Support."
        )

    buttons = [button for button in faq_root.find_all("button") if isinstance(button, Tag)]
    min_questions = int(source.get("min_faq_questions", 5))
    if len(buttons) < min_questions:
        raise SourceContentError(
            f"{source['id']}: seulement {len(buttons)} question(s) FAQ détectée(s), "
            f"minimum attendu {min_questions}."
        )

    min_answer_chars = int(source.get("min_faq_answer_chars", 30))
    answered = 0
    for button in buttons:
        answer_text = _answer_text_for_button(soup, button)
        if len(answer_text) >= min_answer_chars:
            answered += 1

    min_answered = int(source.get("min_answered_faqs", 3))
    if answered < min_answered:
        raise SourceContentError(
            f"{source['id']}: {answered} réponse(s) FAQ exploitable(s) détectée(s) sur "
            f"{len(buttons)} question(s), minimum attendu {min_answered}. "
            "Le HTML initial est insuffisant ; une extraction client-side validée est nécessaire "
            "avant indexation."
        )


def _validate_news_article(source: dict, html: str) -> None:
    parsed_url = urlparse(str(source.get("url") or ""))
    if parsed_url.hostname not in {"etpos.fr", "www.etpos.fr"} or not parsed_url.path.startswith("/fr/blog/"):
        raise SourceContentError(
            f"{source['id']}: une actualité française doit provenir explicitement de https://etpos.fr/fr/blog/."
        )

    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("article") or soup.select_one("main")
    if not isinstance(root, Tag):
        raise SourceContentError(f"{source['id']}: aucun contenu principal d'article n'a été détecté.")

    title_node = root.find("h1") or soup.find("h1")
    title = title_node.get_text(" ", strip=True) if isinstance(title_node, Tag) else ""
    if len(title) < int(source.get("min_article_title_chars", 20)):
        raise SourceContentError(f"{source['id']}: titre d'actualité absent ou trop court.")

    required_title_terms = source.get("required_title_terms", [])
    if not isinstance(required_title_terms, list) or not all(
        isinstance(term, str) and term.strip() for term in required_title_terms
    ):
        raise SourceContentError(f"{source['id']}: required_title_terms doit être une liste de chaînes non vides.")
    title_casefold = title.casefold()
    missing_terms = [term for term in required_title_terms if term.casefold() not in title_casefold]
    if missing_terms:
        raise SourceContentError(
            f"{source['id']}: le titre ne contient pas les termes attendus : {', '.join(missing_terms)}."
        )

    article_text = root.get_text(" ", strip=True)
    min_article_chars = int(source.get("min_article_chars", 600))
    if len(article_text) < min_article_chars:
        raise SourceContentError(
            f"{source['id']}: contenu d'actualité trop court ({len(article_text)} caractères, "
            f"minimum attendu {min_article_chars})."
        )

    time_node = root.find("time")
    time_value = ""
    if isinstance(time_node, Tag):
        time_value = f"{time_node.get('datetime', '')} {time_node.get_text(' ', strip=True)}"
    date_haystack = f"{time_value} {article_text}"
    publication_patterns = (
        r"\b20\d{2}[./-]\d{1,2}[./-]\d{1,2}\b",
        r"\b\d{1,2}[./-]\d{1,2}[./-]20\d{2}\b",
    )
    if not any(re.search(pattern, date_haystack) for pattern in publication_patterns):
        raise SourceContentError(f"{source['id']}: aucune date de publication exploitable n'a été détectée.")


def validate_source_html(source: dict, html: str) -> None:
    profile = str(source.get("validation_profile") or "").strip().lower()
    if not profile:
        return
    if profile == "support_faq_answers":
        _validate_support_faq_answers(source, html)
        return
    if profile == "news_article":
        _validate_news_article(source, html)
        return
    raise SourceContentError(f"{source['id']}: profil de validation inconnu : {profile}")
