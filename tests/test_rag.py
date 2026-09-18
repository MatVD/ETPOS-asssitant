from __future__ import annotations

import pytest

import etpos_assistant.rag as rag_module
from etpos_assistant.providers.mock import MockProvider
from etpos_assistant.retrieval import RetrievedSection
from etpos_assistant.rag import (
    _history_before_current_question,
    _history_for_prompt,
    _retrieval_question,
)


def test_history_before_current_question_removes_only_the_just_saved_turn():
    history = [
        {"role": "user", "content": "Comment gérer les utilisateurs ?"},
        {"role": "assistant", "content": "Réponse précédente"},
        {"role": "user", "content": "Et pour le supprimer ?"},
    ]

    previous = _history_before_current_question(history, "Et pour le supprimer ?")

    assert previous == history[:-1]
    assert _retrieval_question("Et pour le supprimer ?", previous) == (
        "Comment gérer les utilisateurs ? Et pour le supprimer ?"
    )


def test_history_for_prompt_removes_old_source_markers_from_assistant_messages():
    history = [
        {"role": "user", "content": "Comment faire une sauvegarde ?"},
        {"role": "assistant", "content": "Allez dans Système > Sauvegarde. [S1] [S2]"},
    ]

    sanitized = _history_for_prompt(history)

    assert sanitized[0] == history[0]
    assert sanitized[1]["content"] == "Allez dans Système > Sauvegarde.  "


def test_retrieval_question_uses_previous_user_turn_for_short_follow_up_without_concept():
    history = [
        {"role": "user", "content": "Comment gérer les utilisateurs ?"},
        {"role": "assistant", "content": "Réponse précédente [S1]"},
    ]

    query = _retrieval_question("Et pour le supprimer ?", history)

    assert query == "Comment gérer les utilisateurs ? Et pour le supprimer ?"


def test_retrieval_question_keeps_explicit_domain_question_unchanged():
    history = [{"role": "user", "content": "Comment gérer les utilisateurs ?"}]
    question = "Comment ajouter un nouveau compte ?"

    assert _retrieval_question(question, history) == question


def test_retrieval_question_does_not_force_context_on_detailed_standalone_question():
    history = [{"role": "user", "content": "Comment gérer les utilisateurs ?"}]
    question = "Où se trouve le menu permettant de lancer une sauvegarde manuelle complète ?"

    assert _retrieval_question(question, history) == question


@pytest.mark.asyncio
async def test_stream_chat_exposes_real_search_and_generation_statuses(monkeypatch):
    section = RetrievedSection(
        id=1,
        title="Sauvegarde",
        heading_path="SÉCURITÉ ET FIABILITÉ > Sauvegarde",
        source_url="https://example.invalid/sauvegarde",
        source_text="Utilisez Système > Sauvegarde.",
        document_name="Manuel ETPOS",
        document_version="V5.34",
        revision_date="2026-01-30",
        document_hash="hash",
        score=-1.0,
    )
    monkeypatch.setattr(
        rag_module,
        "_history",
        lambda _conversation_id: [{"role": "user", "content": "Comment sauvegarder ?"}],
    )
    monkeypatch.setattr(rag_module, "search_sections", lambda _query, limit: [section])
    monkeypatch.setattr(rag_module, "get_provider", MockProvider)

    events = [
        event
        async for event in rag_module.stream_chat(1, "Comment sauvegarder ?")
    ]

    assert events[0] == {
        "type": "status",
        "stage": "searching",
        "text": "Recherche dans la documentation ETPOS…",
    }
    assert events[1] == {
        "type": "status",
        "stage": "generating",
        "text": "Sources trouvées. Génération de la réponse…",
    }
    assert any(event["type"] == "delta" for event in events[2:-1])
    assert events[-1]["type"] == "done"
    assert events[-1]["citations"][0]["source_id"] == "S1"
