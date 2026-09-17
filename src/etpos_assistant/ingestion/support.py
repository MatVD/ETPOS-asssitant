from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .parser import ParsedDocument, ParsedSection
from .validation import SourceContentError


@dataclass(frozen=True)
class SupportFaqItem:
    question: str
    answer: str


def find_support_component_url(html: str, page_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    for island in soup.select("astro-island[component-url]"):
        if not isinstance(island, Tag):
            continue
        component_url = str(island.get("component-url") or "").strip()
        if not component_url:
            continue
        if island.get("component-export") == "PageIsland":
            candidates.insert(0, component_url)
        else:
            candidates.append(component_url)

    for component_url in candidates:
        if "appbridge" in component_url.lower():
            return urljoin(page_url, component_url)
    if candidates:
        return urljoin(page_url, candidates[0])
    raise SourceContentError("Aucun bundle de composant Support n'a été trouvé dans la page.")


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _read_js_string(text: str, index: int) -> tuple[str, int]:
    if index >= len(text) or text[index] not in {'"', "'", "`"}:
        raise SourceContentError("Chaîne JavaScript statique attendue dans le bundle Support.")
    delimiter = text[index]
    index += 1
    result: list[str] = []
    escapes = {
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "b": "\b",
        "f": "\f",
        "v": "\v",
        "0": "\0",
        "\\": "\\",
        '"': '"',
        "'": "'",
        "`": "`",
        "/": "/",
    }
    while index < len(text):
        char = text[index]
        if char == delimiter:
            return "".join(result), index + 1
        if delimiter == "`" and text.startswith("${", index):
            raise SourceContentError(
                "Interpolation JavaScript détectée dans une réponse FAQ ; extraction statique refusée."
            )
        if char != "\\":
            result.append(char)
            index += 1
            continue

        index += 1
        if index >= len(text):
            raise SourceContentError("Échappement JavaScript incomplet dans le bundle Support.")
        escaped = text[index]
        if escaped in "\r\n":
            if escaped == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                index += 1
            index += 1
            continue
        if escaped == "u":
            raw = text[index + 1 : index + 5]
            if len(raw) != 4:
                raise SourceContentError("Échappement Unicode JavaScript incomplet.")
            try:
                result.append(chr(int(raw, 16)))
            except ValueError as exc:
                raise SourceContentError("Échappement Unicode JavaScript invalide.") from exc
            index += 5
            continue
        if escaped == "x":
            raw = text[index + 1 : index + 3]
            if len(raw) != 2:
                raise SourceContentError("Échappement hexadécimal JavaScript incomplet.")
            try:
                result.append(chr(int(raw, 16)))
            except ValueError as exc:
                raise SourceContentError("Échappement hexadécimal JavaScript invalide.") from exc
            index += 3
            continue
        result.append(escapes.get(escaped, escaped))
        index += 1
    raise SourceContentError("Chaîne JavaScript non terminée dans le bundle Support.")


def _object_spans(array_text: str) -> list[str]:
    spans: list[str] = []
    index = 0
    depth = 0
    start: int | None = None
    while index < len(array_text):
        char = array_text[index]
        if char in {'"', "'", "`"}:
            _value, index = _read_js_string(array_text, index)
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                raise SourceContentError("Structure FAQ JavaScript invalide.")
            if depth == 0 and start is not None:
                spans.append(array_text[start : index + 1])
                start = None
        index += 1
    if depth != 0:
        raise SourceContentError("Objet FAQ JavaScript non terminé.")
    return spans


def _find_array_end(text: str, start: int) -> int:
    depth = 1
    index = start
    while index < len(text):
        char = text[index]
        if char in {'"', "'", "`"}:
            _value, index = _read_js_string(text, index)
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise SourceContentError("Tableau FAQ français non terminé dans le bundle Support.")


def _read_property(obj: str, key: str) -> str:
    marker = f"{key}:"
    index = obj.find(marker)
    if index < 0:
        raise SourceContentError(f"Propriété {key} absente d'une entrée FAQ Support.")
    index = _skip_space(obj, index + len(marker))
    value, _end = _read_js_string(obj, index)
    return " ".join(value.split())


def extract_french_faq_items(bundle_text: str) -> list[SupportFaqItem]:
    marker = "fr:{faqs:["
    marker_index = bundle_text.find(marker)
    if marker_index < 0:
        raise SourceContentError("Bloc fr:{faqs:[ introuvable dans le bundle Support.")
    array_start = marker_index + len(marker)
    array_end = _find_array_end(bundle_text, array_start)
    items: list[SupportFaqItem] = []
    for obj in _object_spans(bundle_text[array_start:array_end]):
        question = _read_property(obj, "question")
        answer = _read_property(obj, "answer")
        if question and answer:
            items.append(SupportFaqItem(question=question, answer=answer))
    if not items:
        raise SourceContentError("Aucune question/réponse française extraite du bundle Support.")
    return items


def validate_faq_items(source: dict, items: list[SupportFaqItem]) -> None:
    min_questions = int(source.get("min_faq_questions", 5))
    if len(items) < min_questions:
        raise SourceContentError(
            f"{source['id']}: seulement {len(items)} question(s)/réponse(s) FAQ extraite(s), "
            f"minimum attendu {min_questions}."
        )
    min_answer_chars = int(source.get("min_faq_answer_chars", 30))
    answered = sum(len(item.answer) >= min_answer_chars for item in items)
    min_answered = int(source.get("min_answered_faqs", 3))
    if answered < min_answered:
        raise SourceContentError(
            f"{source['id']}: seulement {answered} réponse(s) FAQ suffisamment détaillée(s), "
            f"minimum attendu {min_answered}."
        )


def faq_items_to_document(items: list[SupportFaqItem], page_url: str) -> ParsedDocument:
    base = page_url.split("#", 1)[0]
    sections = [
        ParsedSection(
            order=index,
            title=item.question,
            heading_path=f"Support ETPOS > FAQ > {item.question}",
            anchor="faqs",
            source_url=f"{base}#faqs",
            source_text=f"{item.question}\n{item.answer}",
            search_text=f"{item.question} {item.answer}",
            image_refs=[],
        )
        for index, item in enumerate(items, start=1)
    ]
    return ParsedDocument(
        title="Support ETPOS - FAQ - Français",
        version=None,
        revision_date=None,
        sections=sections,
    )


def ensure_same_origin_asset(page_url: str, asset_url: str) -> None:
    page = urlparse(page_url)
    asset = urlparse(asset_url)
    if page.scheme != "https" or asset.scheme != "https" or page.hostname != asset.hostname:
        raise SourceContentError(
            f"Le bundle Support doit rester sur le même hôte officiel : {asset_url}"
        )


def parse_support_client_bundle(
    source: dict,
    page_html: str,
    bundle_text: str,
) -> tuple[ParsedDocument, str, int]:
    component_url = find_support_component_url(page_html, source["url"])
    ensure_same_origin_asset(source["url"], component_url)
    items = extract_french_faq_items(bundle_text)
    validate_faq_items(source, items)
    return faq_items_to_document(items, source["url"]), component_url, len(items)


def composite_support_digest(page_digest: str, bundle_digest: str, component_url: str) -> str:
    digest = hashlib.sha256()
    for value in (page_digest, component_url, bundle_digest):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def support_snapshot_payload(page_url: str, page_html: str, component_url: str, bundle_text: str) -> str:
    return json.dumps(
        {
            "page_url": page_url,
            "page_html": page_html,
            "component_url": component_url,
            "component_javascript": bundle_text,
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"
