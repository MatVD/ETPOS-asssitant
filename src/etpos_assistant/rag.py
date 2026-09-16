from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from .config import settings
from .db import app_db
from .markdown import render_safe_markdown
from .providers import CodexCliProvider, MockProvider, SourceContext
from .retrieval import RetrievedSection, search_sections

CITATION_RE = re.compile(r"\[S(\d+)\]")
ABSTENTION = "La documentation ETPOS actuellement indexée ne permet pas de répondre avec certitude à cette question."


def get_provider():
    if settings.provider == "codex":
        return CodexCliProvider()
    return MockProvider()


def _history(conversation_id: int) -> list[dict[str, str]]:
    with app_db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 8",
            (conversation_id,),
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def _citations(answer: str, sections: list[RetrievedSection]) -> list[dict]:
    requested = []
    for match in CITATION_RE.finditer(answer):
        idx = int(match.group(1)) - 1
        if 0 <= idx < len(sections) and idx not in requested:
            requested.append(idx)
    result = []
    for idx in requested:
        section = sections[idx]
        result.append(
            {
                "source_id": f"S{idx + 1}",
                "section_id": section.id,
                "title": section.title,
                "heading_path": section.heading_path,
                "document_name": section.document_name,
                "version": section.document_version,
                "revision_date": section.revision_date,
                "official_url": section.source_url,
                "internal_url": f"/sources/{section.id}",
            }
        )
    return result


def _remove_invalid_citations(answer: str, count: int) -> str:
    def repl(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return match.group(0) if 1 <= idx <= count else ""
    return CITATION_RE.sub(repl, answer)


async def stream_chat(conversation_id: int, question: str) -> AsyncIterator[dict]:
    sections = search_sections(question, limit=6)
    if not sections:
        yield {"type": "delta", "text": ABSTENTION}
        yield {
            "type": "done",
            "text": ABSTENTION,
            "html": render_safe_markdown(ABSTENTION),
            "citations": [],
        }
        return

    contexts = [
        SourceContext(
            source_id=f"S{i + 1}",
            title=section.title,
            heading_path=section.heading_path,
            text=section.source_text[:9000],
        )
        for i, section in enumerate(sections)
    ]
    provider = get_provider()
    chunks: list[str] = []
    async for chunk in provider.stream_answer(
        question=question,
        sources=contexts,
        history=_history(conversation_id),
    ):
        chunks.append(chunk)
        if sum(len(part) for part in chunks) > 40000:
            break
        yield {"type": "delta", "text": chunk}

    answer = _remove_invalid_citations("".join(chunks).strip(), len(sections))
    if not answer:
        answer = ABSTENTION
    citations = _citations(answer, sections)
    yield {
        "type": "done",
        "text": answer,
        "html": render_safe_markdown(answer),
        "citations": citations,
    }


def save_assistant_message(conversation_id: int, text: str, citations: list[dict]) -> None:
    now = datetime.now(UTC).isoformat()
    with app_db() as conn:
        conn.execute(
            "INSERT INTO messages(conversation_id, role, content, citations_json, created_at) VALUES (?, 'assistant', ?, ?, ?)",
            (conversation_id, text, json.dumps(citations, ensure_ascii=False), now),
        )
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
