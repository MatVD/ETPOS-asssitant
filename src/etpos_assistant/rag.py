from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from .citations import normalize_citation_link
from .config import settings
from .db import app_db
from .markdown import render_safe_markdown
from .providers import CodexCliProvider, MockProvider, SourceContext
from .providers.prompting import ABSTENTION_TEXT
from .retrieval import RetrievedSection, search_sections
from .vocabulary import analyze_query, normalize_domain_text

CITATION_RE = re.compile(r"\[S(\d+)\]")
ABSTENTION = ABSTENTION_TEXT
logger = logging.getLogger(__name__)


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


def _history_for_prompt(history: list[dict[str, str]]) -> list[dict[str, str]]:
    sanitized: list[dict[str, str]] = []
    for item in history:
        content = item["content"]
        if item["role"] == "assistant":
            content = CITATION_RE.sub("", content)
        sanitized.append({"role": item["role"], "content": content})
    return sanitized


def _history_before_current_question(
    history: list[dict[str, str]],
    question: str,
) -> list[dict[str, str]]:
    if (
        history
        and history[-1]["role"] == "user"
        and history[-1]["content"].strip() == question.strip()
    ):
        return history[:-1]
    return history


def _retrieval_question(question: str, history: list[dict[str, str]]) -> str:
    analysis = analyze_query(question)
    if analysis.concepts:
        return question

    normalized = normalize_domain_text(question)
    content_tokens = [token for token in normalized.split() if len(token) >= 3]
    follow_up_markers = {
        "ca",
        "cela",
        "celui",
        "celle",
        "ceux",
        "elles",
        "eux",
        "leur",
        "leurs",
        "lui",
        "supprimer",
        "modifier",
        "changer",
        "ajouter",
        "creer",
    }
    looks_contextual = len(content_tokens) <= 6 or bool(set(content_tokens) & follow_up_markers)
    if not looks_contextual:
        return question

    for item in reversed(history):
        if item["role"] == "user" and item["content"].strip():
            return f"{item['content'].strip()} {question.strip()}"
    return question


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
            normalize_citation_link(
                {
                    "source_id": f"S{idx + 1}",
                    "section_id": section.id,
                    "title": section.title,
                    "heading_path": section.heading_path,
                    "document_name": section.document_name,
                    "version": section.document_version,
                    "revision_date": section.revision_date,
                    "document_hash": section.document_hash,
                    "official_url": section.source_url,
                }
            )
        )
    return result


def _remove_invalid_citations(answer: str, count: int) -> str:
    def repl(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return match.group(0) if 1 <= idx <= count else ""
    return CITATION_RE.sub(repl, answer)


def finalize_answer(raw_answer: str, sections: list[RetrievedSection]) -> tuple[str, list[dict]]:
    answer = _remove_invalid_citations(raw_answer.strip(), len(sections))
    if not answer:
        answer = ABSTENTION
    return answer, _citations(answer, sections)


async def stream_chat(conversation_id: int, question: str) -> AsyncIterator[dict]:
    total_started = time.perf_counter()
    history = _history_before_current_question(_history(conversation_id), question)
    retrieval_question = _retrieval_question(question, history)

    yield {
        "type": "status",
        "stage": "searching",
        "text": "Recherche dans la documentation ETPOS…",
    }
    retrieval_started = time.perf_counter()
    sections = search_sections(retrieval_question, limit=settings.retrieval_limit)
    retrieval_latency_ms = (time.perf_counter() - retrieval_started) * 1000.0

    if not sections:
        total_latency_ms = (time.perf_counter() - total_started) * 1000.0
        logger.info(
            "chat_performance %s",
            json.dumps(
                {
                    "conversation_id": conversation_id,
                    "retrieval_latency_ms": round(retrieval_latency_ms, 3),
                    "generation_latency_ms": 0.0,
                    "total_latency_ms": round(total_latency_ms, 3),
                    "source_count": 0,
                    "question_chars": len(question),
                    "retrieval_question_chars": len(retrieval_question),
                    "answer_chars": len(ABSTENTION),
                },
                ensure_ascii=False,
            ),
        )
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
            text=section.source_text[: settings.source_char_limit],
        )
        for i, section in enumerate(sections)
    ]
    prompt_history = _history_for_prompt(history)
    yield {
        "type": "status",
        "stage": "generating",
        "text": "Sources trouvées. Génération de la réponse…",
    }

    provider = get_provider()
    chunks: list[str] = []
    generation_started = time.perf_counter()
    async for chunk in provider.stream_answer(
        question=question,
        sources=contexts,
        history=prompt_history,
    ):
        chunks.append(chunk)
        if sum(len(part) for part in chunks) > 40000:
            break
        yield {"type": "delta", "text": chunk}
    generation_latency_ms = (time.perf_counter() - generation_started) * 1000.0

    finalization_started = time.perf_counter()
    answer, citations = finalize_answer("".join(chunks), sections)
    html = render_safe_markdown(answer)
    finalization_latency_ms = (time.perf_counter() - finalization_started) * 1000.0
    total_latency_ms = (time.perf_counter() - total_started) * 1000.0

    performance = {
        "conversation_id": conversation_id,
        "retrieval_latency_ms": round(retrieval_latency_ms, 3),
        "generation_latency_ms": round(generation_latency_ms, 3),
        "finalization_latency_ms": round(finalization_latency_ms, 3),
        "total_latency_ms": round(total_latency_ms, 3),
        "source_count": len(sections),
        "source_chars": sum(len(context.text) for context in contexts),
        "history_messages": len(prompt_history),
        "history_chars": sum(len(item["content"]) for item in prompt_history),
        "question_chars": len(question),
        "retrieval_question_chars": len(retrieval_question),
        "answer_chars": len(answer),
    }
    provider_metrics = getattr(provider, "last_metrics", None)
    if provider_metrics is not None and hasattr(provider_metrics, "as_dict"):
        performance["codex"] = provider_metrics.as_dict()
    logger.info("chat_performance %s", json.dumps(performance, ensure_ascii=False))

    yield {
        "type": "done",
        "text": answer,
        "html": html,
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
