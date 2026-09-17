from __future__ import annotations

from etpos_assistant.rag import _history_for_prompt, _retrieval_question


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
