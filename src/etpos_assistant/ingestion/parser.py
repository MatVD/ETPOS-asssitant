from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag


@dataclass(frozen=True)
class ParsedSection:
    order: int
    title: str
    heading_path: str
    anchor: str | None
    source_url: str
    source_text: str
    search_text: str
    image_refs: list[dict[str, str]]


@dataclass(frozen=True)
class ParsedDocument:
    title: str
    version: str | None
    revision_date: str | None
    sections: list[ParsedSection]


MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()


def _detect_version(text: str) -> str | None:
    match = re.search(r"\bV\s*(\d+(?:\.\d+){1,3})\b", text, flags=re.IGNORECASE)
    return f"V{match.group(1)}" if match else None


def _detect_publication_date(soup: BeautifulSoup) -> str | None:
    node = soup.find("meta", attrs={"property": "article:published_time"})
    if not isinstance(node, Tag):
        return None
    value = str(node.get("content") or "").strip()
    match = re.match(r"(20\d{2})-(\d{1,2})-(\d{1,2})", value)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def _detect_revision(text: str) -> str | None:
    numeric = re.search(
        r"(?:dernière\s+)?(?:révision|revision|mis(?:e)?\s+à\s+jour)[^0-9]{0,40}(\d{1,2})[/-](\d{1,2})[/-](20\d{2})",
        text,
        flags=re.IGNORECASE,
    )
    if numeric:
        try:
            return date(int(numeric.group(3)), int(numeric.group(2)), int(numeric.group(1))).isoformat()
        except ValueError:
            pass

    pattern = r"(?:révision|revision|mis(?:e)?\s+à\s+jour)[^\n]{0,80}?(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})", text, flags=re.IGNORECASE)
    if not match:
        return None
    day = int(match.group(1))
    month_name = unicodedata.normalize("NFKC", match.group(2).lower())
    month = MONTHS.get(month_name)
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, day).isoformat()
    except ValueError:
        return None


def _best_root(soup: BeautifulSoup) -> Tag:
    selectors = ["main", "article", "#content", ".main-content", ".content", "body"]
    for selector in selectors:
        node = soup.select_one(selector)
        if isinstance(node, Tag) and len(node.get_text(" ", strip=True)) > 500:
            return node
    return soup


def parse_html(html: str, source_url: str) -> ParsedDocument:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style, noscript, nav, header, footer, form, iframe"):
        node.decompose()

    root = _best_root(soup)
    full_text = _normalized(root.get_text("\n", strip=True))
    title = _normalized(soup.title.get_text(" ", strip=True)) if soup.title else "Documentation ETPOS"
    version = _detect_version(full_text)
    revision = _detect_revision(full_text) or _detect_publication_date(soup)

    sections: list[ParsedSection] = []
    stack: dict[int, str] = {}
    current_title = title
    current_anchor: str | None = None
    current_level = 1
    buffer: list[str] = []
    images: list[dict[str, str]] = []
    seen_heading = False

    def flush() -> None:
        nonlocal buffer, images
        text = _normalized("\n".join(buffer))
        if len(text) < 40:
            buffer = []
            images = []
            return
        heading_path = " > ".join(stack[level] for level in sorted(stack) if level <= current_level and stack[level])
        if not heading_path:
            heading_path = current_title
        anchor = current_anchor
        url = source_url + (f"#{anchor}" if anchor else "")
        sections.append(
            ParsedSection(
                order=len(sections) + 1,
                title=current_title,
                heading_path=heading_path,
                anchor=anchor,
                source_url=url,
                source_text=text,
                search_text=_normalized(f"{current_title} {heading_path} {text}"),
                image_refs=list(images),
            )
        )
        buffer = []
        images = []

    interesting = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "img"}
    for node in root.find_all(list(interesting)):
        if not isinstance(node, Tag):
            continue
        if node.name in {"p", "li", "table"}:
            parent = node.parent
            nested = False
            while isinstance(parent, Tag) and parent is not root:
                if parent.name in {"p", "li", "table"}:
                    nested = True
                    break
                parent = parent.parent
            if nested:
                continue
        if node.name and re.fullmatch(r"h[1-6]", node.name):
            if seen_heading:
                flush()
            else:
                buffer = []
                images = []
                seen_heading = True
            current_level = int(node.name[1])
            current_title = _normalized(node.get_text(" ", strip=True)) or "Section"
            current_anchor = node.get("id") or None
            stack[current_level] = current_title
            for level in list(stack):
                if level > current_level:
                    stack.pop(level, None)
            continue
        if node.name == "img":
            src = node.get("src")
            if src:
                images.append({"url": urljoin(source_url, src), "alt": _normalized(node.get("alt", ""))})
            continue
        text = _normalized(node.get_text(" ", strip=True))
        if text:
            buffer.append(text)

    flush()

    if not sections and full_text:
        sections.append(
            ParsedSection(
                order=1,
                title=title,
                heading_path=title,
                anchor=None,
                source_url=source_url,
                source_text=full_text,
                search_text=full_text,
                image_refs=[],
            )
        )

    return ParsedDocument(title=title, version=version, revision_date=revision, sections=sections)
