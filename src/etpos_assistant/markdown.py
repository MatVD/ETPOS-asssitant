from __future__ import annotations

from markdown_it import MarkdownIt

_MD = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
_MD.disable(["link", "image", "autolink"])


def render_safe_markdown(text: str) -> str:
    # Raw HTML is escaped and model-provided links/images are deliberately not activated.
    # Citations are rendered separately from trusted backend metadata.
    return _MD.render(text)
