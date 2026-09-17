from __future__ import annotations

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


def validate_source_html(source: dict, html: str) -> None:
    profile = str(source.get("validation_profile") or "").strip().lower()
    if not profile:
        return
    if profile == "support_faq_answers":
        _validate_support_faq_answers(source, html)
        return
    raise SourceContentError(f"{source['id']}: profil de validation inconnu : {profile}")
