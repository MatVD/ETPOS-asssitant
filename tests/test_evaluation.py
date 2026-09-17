from __future__ import annotations

import json
from pathlib import Path

import pytest

import etpos_assistant.evaluation as evaluation
from etpos_assistant.evaluation import (
    BenchmarkError,
    GeneratedAnswer,
    evaluate_retrieval_case,
    load_benchmark,
    score_answer_case,
    summarize_answers,
    summarize_retrieval,
)
from etpos_assistant.rag import ABSTENTION
from etpos_assistant.retrieval import RetrievedSection


def _section(section_id: int, title: str, path: str, text: str = "") -> RetrievedSection:
    return RetrievedSection(
        id=section_id,
        title=title,
        heading_path=path,
        source_url="https://example.test/manual",
        source_text=text,
        document_name="ETPOS",
        document_version="5.34",
        revision_date="2026-01-30",
        document_hash="test-document-hash",
        score=-1.0,
    )


def test_load_benchmark_supports_rich_schema_and_legacy_fields(tmp_path):
    path = tmp_path / "benchmark.jsonl"
    rows = [
        {
            "id": "rich",
            "category": "menu_path",
            "question": "Où ?",
            "answerability": "full",
            "relevant_section_groups": [["Types de règlement"]],
            "expected_heading_paths": ["CONFIGURER ETPOS > Types de Règlement"],
            "expected_menu_paths": ["Système > Configurations > Types de règlement"],
            "required_facts": ["Système + Configurations"],
        },
        {
            "question": "Ancien format ?",
            "answerable": True,
            "expected_section_keywords": ["sauvegarde"],
        },
        {
            "id": "none",
            "category": "unanswerable",
            "question": "Hors documentation ?",
            "answerability": "none",
            "relevant_section_groups": [],
        },
    ]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

    cases = load_benchmark(path)

    assert [case.case_id for case in cases] == ["rich", "line-2", "none"]
    assert cases[0].expected_heading_paths == ("CONFIGURER ETPOS > Types de Règlement",)
    assert cases[0].expected_menu_paths == ("Système > Configurations > Types de règlement",)
    assert cases[1].relevant_section_groups == (("sauvegarde",),)
    assert cases[2].answerability == "none"


def test_project_benchmark_covers_required_categories():
    path = Path(__file__).resolve().parents[1] / "eval" / "benchmark.jsonl"
    cases = load_benchmark(path)

    assert len(cases) == 47
    case_ids = {case.case_id for case in cases}
    assert {"simple-families", "simple-peripherals"} <= case_ids
    assert {case.category for case in cases} >= {
        "simple",
        "procedure",
        "menu_path",
        "synonym",
        "ambiguous",
        "partial",
        "unanswerable",
    }
    assert sum(case.answerability == "none" for case in cases) >= 5
    assert any(case.expected_menu_paths for case in cases)
    assert any(case.required_facts for case in cases)


def test_load_benchmark_rejects_answerable_case_without_expected_section(tmp_path):
    path = tmp_path / "invalid.jsonl"
    path.write_text(
        json.dumps({"id": "bad", "question": "Question", "answerability": "full"}),
        encoding="utf-8",
    )

    with pytest.raises(BenchmarkError, match="relevant_section_group"):
        load_benchmark(path)


def test_retrieval_evaluation_matches_normalized_groups_and_summarizes(monkeypatch):
    cases = [
        evaluation.BenchmarkCase(
            case_id="permissions",
            category="procedure",
            question="permissions",
            answerability="full",
            relevant_section_groups=(("Définir les permissions",),),
        ),
        evaluation.BenchmarkCase(
            case_id="balance",
            category="procedure",
            question="balance",
            answerability="full",
            relevant_section_groups=(("mode", "balance"), ("périphériques", "dispositifs")),
        ),
        evaluation.BenchmarkCase(
            case_id="none",
            category="unanswerable",
            question="hors doc",
            answerability="none",
            relevant_section_groups=(),
        ),
    ]

    results_by_question = {
        "permissions": [
            _section(1, "Autre", "Autre"),
            _section(2, "Définir les permissions", "GESTION DES UTILISATEURS > Définir les permissions"),
        ],
        "balance": [
            _section(
                3,
                "Activer le mode de fonctionnement type Balance",
                "Balance > Activer le mode de fonctionnement type Balance",
                "Configurer la balance dans Système > Configuration > Périphériques > Dispositifs.",
            )
        ],
        "hors doc": [_section(4, "Sans rapport", "Divers")],
    }
    monkeypatch.setattr(
        evaluation,
        "search_sections",
        lambda question, limit, db_path=None: results_by_question[question][:limit],
    )

    results = [evaluate_retrieval_case(case, 5) for case in cases]
    summary = summarize_retrieval(results, 5)

    assert results[0].first_relevant_rank == 2
    assert results[1].matched_groups == 2
    assert summary["answerable_cases"] == 2
    assert summary["unanswerable_cases"] == 1
    assert summary["recall_at_k"] == 1.0
    assert summary["mrr"] == pytest.approx(0.75)
    assert summary["group_coverage"] == 1.0


def test_answer_scoring_checks_facts_menu_paths_citations_and_abstention():
    relevant = _section(
        10,
        "Types de Règlement",
        "CONFIGURER ETPOS > Types de Règlement",
        "Les types de règlement sont définis dans Système + Configurations + Types de règlement.",
    )
    answerable = evaluation.BenchmarkCase(
        case_id="payment-types",
        category="menu_path",
        question="Où configurer les types de règlement ?",
        answerability="full",
        relevant_section_groups=(("types de règlement",),),
        expected_heading_paths=("CONFIGURER ETPOS > Types de Règlement",),
        expected_menu_paths=("Système > Configurations > Types de règlement",),
        required_facts=("types de règlement",),
    )
    generated = GeneratedAnswer(
        text="Allez dans Système > Configurations > Types de règlement pour définir les types de règlement. [S1]",
        citations=({"source_id": "S1", "section_id": 10},),
        sections=(relevant,),
        retrieval_latency_ms=5.0,
        generation_latency_ms=25.0,
        total_latency_ms=30.0,
    )

    answer_result = score_answer_case(answerable, generated)

    assert answer_result.abstention_correct
    assert answer_result.fact_coverage == 1.0
    assert answer_result.menu_path_coverage == 1.0
    assert answer_result.citation_presence_correct
    assert answer_result.citation_relevance == 1.0

    unanswerable = evaluation.BenchmarkCase(
        case_id="none",
        category="unanswerable",
        question="Quelle est la couleur préférée du développeur ?",
        answerability="none",
        relevant_section_groups=(),
    )
    abstained = GeneratedAnswer(
        text=ABSTENTION,
        citations=(),
        sections=(),
        retrieval_latency_ms=4.0,
        generation_latency_ms=0.0,
        total_latency_ms=4.0,
    )
    abstention_result = score_answer_case(unanswerable, abstained)
    summary = summarize_answers([answer_result, abstention_result])

    assert abstention_result.abstention_correct
    assert abstention_result.citation_presence_correct
    assert summary["abstention_accuracy"] == 1.0
    assert summary["fact_coverage"] == 1.0
    assert summary["menu_path_coverage"] == 1.0
    assert summary["citation_presence_accuracy"] == 1.0
    assert summary["citation_relevance"] == 1.0
    assert summary["latency_p95_ms"] == 30.0


def test_answer_scoring_detects_irrelevant_citation():
    relevant = _section(20, "Sauvegarde", "SÉCURITÉ ET FIABILITÉ > Sauvegarde")
    unrelated = _section(21, "Rapports", "RAPPORTS ET GRAPHIQUES")
    case = evaluation.BenchmarkCase(
        case_id="backup",
        category="simple",
        question="Comment sauvegarder ?",
        answerability="full",
        relevant_section_groups=(("sauvegarde",),),
    )
    generated = GeneratedAnswer(
        text="Utilisez la sauvegarde. [S2]",
        citations=({"source_id": "S2", "section_id": 21},),
        sections=(relevant, unrelated),
        retrieval_latency_ms=2.0,
        generation_latency_ms=3.0,
        total_latency_ms=5.0,
    )

    result = score_answer_case(case, generated)

    assert result.citation_presence_correct
    assert result.citation_relevance == 0.0
