from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import etpos_assistant.evaluation as evaluation
from etpos_assistant.evaluation import (
    BenchmarkError,
    GeneratedAnswer,
    answer_result_to_dict,
    evaluate_retrieval_case,
    load_benchmark,
    rescore_answer_report,
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
    assert sum(case.answerability == "none" for case in cases) >= 3
    assert any(case.expected_menu_paths or case.expected_menu_path_groups for case in cases)
    assert any(case.required_facts or case.required_fact_groups for case in cases)


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
    assert answer_result.citation_expected_coverage == 1.0

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
    assert summary["citation_expected_coverage"] == 1.0
    assert summary["latency_p95_ms"] == 30.0


def test_answer_scoring_accepts_fact_alternatives_and_ordered_menu_segments():
    relevant = _section(30, "Sauvegarde", "SÉCURITÉ ET FIABILITÉ > Sauvegarde")
    case = evaluation.BenchmarkCase(
        case_id="backup-natural",
        category="simple",
        question="Comment sauvegarder ?",
        answerability="full",
        relevant_section_groups=(("sauvegarde",),),
        expected_menu_path_groups=(("Système > Sauvegarde > Exporter",),),
        required_fact_groups=(
            ("définir les options souhaitées", "options souhaitées"),
            ("valider", "validez", "validation"),
        ),
    )
    generated = GeneratedAnswer(
        text=(
            "Allez dans Système > Sauvegarde, puis ouvrez l'onglet Exporter. "
            "Définissez les options souhaitées et validez. [S1]"
        ),
        citations=({"source_id": "S1", "section_id": 30},),
        sections=(relevant,),
        retrieval_latency_ms=1.0,
        generation_latency_ms=2.0,
        total_latency_ms=3.0,
    )

    result = score_answer_case(case, generated)

    assert result.fact_coverage == 1.0
    assert result.menu_path_coverage == 1.0


def test_load_benchmark_rejects_legacy_and_grouped_expectations_together(tmp_path):
    path = tmp_path / "invalid.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "bad-groups",
                "question": "Question",
                "answerability": "full",
                "relevant_section_groups": [["sauvegarde"]],
                "required_facts": ["valider"],
                "required_fact_groups": [["valider", "validation"]],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BenchmarkError, match="required_facts ou required_fact_groups"):
        load_benchmark(path)


def test_acceptance_benchmark_is_separate_and_covers_risk_categories():
    path = Path(__file__).resolve().parents[1] / "eval" / "acceptance.jsonl"
    cases = load_benchmark(path)

    assert len(cases) >= 12
    assert {case.category for case in cases} >= {
        "simple",
        "procedure",
        "menu_path",
        "synonym",
        "ambiguous",
        "partial",
        "unanswerable",
    }
    assert sum(case.answerability == "none" for case in cases) >= 2


def test_support_benchmark_covers_each_official_faq_section():
    path = Path(__file__).resolve().parents[1] / "eval" / "support.jsonl"
    cases = load_benchmark(path)

    assert len(cases) == 10
    assert {case.category for case in cases} == {"source_contract"}
    assert all(case.answerability == "full" for case in cases)
    assert all(case.relevant_section_groups for case in cases)
    assert all(case.required_fact_groups for case in cases)


def test_answer_result_to_dict_keeps_review_evidence():
    relevant = _section(10, "Types de Règlement", "CONFIGURER ETPOS > Types de Règlement")
    case = evaluation.BenchmarkCase(
        case_id="payment-types",
        category="menu_path",
        question="Où configurer les types de règlement ?",
        answerability="full",
        relevant_section_groups=(("types de règlement",),),
    )
    generated = GeneratedAnswer(
        text="Réponse [S1]",
        citations=({"source_id": "S1", "section_id": 10},),
        sections=(relevant,),
        retrieval_latency_ms=5.0,
        generation_latency_ms=25.0,
        total_latency_ms=30.0,
    )

    payload = answer_result_to_dict(score_answer_case(case, generated))

    assert payload["id"] == "payment-types"
    assert payload["answer"] == "Réponse [S1]"
    assert payload["citations"][0]["section_id"] == 10
    assert payload["retrieved_sections"][0]["heading_path"] == "CONFIGURER ETPOS > Types de Règlement"
    assert payload["total_latency_ms"] == 30.0


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
    assert result.citation_expected_coverage == 0.0


def test_rescore_answer_report_reuses_saved_answers_without_provider(tmp_path):
    benchmark = tmp_path / "benchmark.jsonl"
    benchmark.write_text(
        json.dumps(
            {
                "id": "backup",
                "category": "simple",
                "question": "Comment sauvegarder ?",
                "answerability": "full",
                "relevant_section_groups": [["sauvegarde"]],
                "expected_menu_path_groups": [["Système > Sauvegarde > Exporter"]],
                "required_fact_groups": [["valider", "validez"]],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    docs_database = tmp_path / "docs.db"
    with sqlite3.connect(docs_database) as conn:
        conn.execute(
            "CREATE TABLE documents(id INTEGER PRIMARY KEY, content_hash TEXT NOT NULL)"
        )
        conn.execute(
            """
            CREATE TABLE sections(
                id INTEGER PRIMARY KEY,
                document_id INTEGER NOT NULL,
                heading_path TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_text TEXT NOT NULL
            )
            """
        )
        conn.execute("INSERT INTO documents(id, content_hash) VALUES (?, ?)", (1, "hash"))
        conn.execute(
            """
            INSERT INTO sections(id, document_id, heading_path, source_url, source_text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                99,
                1,
                "SÉCURITÉ ET FIABILITÉ > Sauvegarde",
                "https://example.test/manual#backup",
                "Procédure de sauvegarde via l'onglet Exporter.",
            ),
        )

    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "question": "Comment sauvegarder ?",
                        "answer": "Allez dans Système > Sauvegarde, puis ouvrez Exporter et validez. [S1]",
                        "citations": [{"source_id": "S1", "section_id": 10}],
                        "retrieval_latency_ms": 1.0,
                        "generation_latency_ms": 2.0,
                        "total_latency_ms": 3.0,
                        "retrieved_sections": [
                            {
                                "section_id": 10,
                                "title": "Sauvegarde",
                                "heading_path": "SÉCURITÉ ET FIABILITÉ > Sauvegarde",
                                "source_url": "https://example.test/manual#backup",
                                "document_name": "ETPOS",
                                "document_version": "5.34",
                                "revision_date": "2026-01-30",
                                "document_hash": "hash",
                                "score": -1.0,
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    results, summary = rescore_answer_report(benchmark, report, docs_database)

    assert len(results) == 1
    assert summary["fact_coverage"] == 1.0
    assert summary["menu_path_coverage"] == 1.0
    assert summary["citation_presence_accuracy"] == 1.0
    assert summary["citation_relevance"] == 1.0
    assert summary["citation_expected_coverage"] == 1.0
