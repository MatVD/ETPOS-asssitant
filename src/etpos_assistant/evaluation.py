from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .providers import SourceContext
from .rag import (
    ABSTENTION,
    _history_for_prompt,
    _retrieval_question,
    finalize_answer,
    get_provider,
)
from .retrieval import RetrievedSection, search_sections
from .vocabulary import normalize_domain_text


ANSWERABILITY_VALUES = {"full", "partial", "none"}

FACT_STOPWORDS = {
    "a", "au", "aux", "avec", "ce", "ces", "dans", "de", "des", "du", "en", "et",
    "est", "la", "le", "les", "ou", "par", "pour", "sur", "un", "une", "vers",
}


class BenchmarkError(ValueError):
    pass


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    category: str
    question: str
    answerability: str
    relevant_section_groups: tuple[tuple[str, ...], ...]
    expected_citation_groups: tuple[tuple[str, ...], ...] = ()
    expected_heading_paths: tuple[str, ...] = ()
    expected_menu_paths: tuple[str, ...] = ()
    expected_menu_path_groups: tuple[tuple[str, ...], ...] = ()
    required_facts: tuple[str, ...] = ()
    required_fact_groups: tuple[tuple[str, ...], ...] = ()
    history: tuple[dict[str, str], ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class RetrievalCaseResult:
    case: BenchmarkCase
    first_relevant_rank: int | None
    matched_groups: int
    expected_groups: int
    latency_ms: float

    @property
    def hit(self) -> bool:
        return self.first_relevant_rank is not None

    @property
    def reciprocal_rank(self) -> float:
        if self.first_relevant_rank is None:
            return 0.0
        return 1.0 / self.first_relevant_rank

    @property
    def group_coverage(self) -> float:
        if not self.expected_groups:
            return 0.0
        return self.matched_groups / self.expected_groups


@dataclass(frozen=True)
class GeneratedAnswer:
    text: str
    citations: tuple[dict, ...]
    sections: tuple[RetrievedSection, ...]
    retrieval_latency_ms: float
    generation_latency_ms: float
    total_latency_ms: float
    provider_metrics: dict | None = None
    answer_status: str | None = None


@dataclass(frozen=True)
class AnswerCaseResult:
    case: BenchmarkCase
    answer: GeneratedAnswer
    abstention_correct: bool
    answer_status_correct: bool | None
    matched_facts: int
    expected_facts: int
    matched_menu_paths: int
    expected_menu_paths: int
    relevant_citations: int
    total_citations: int
    matched_citation_groups: int
    expected_citation_groups: int
    matched_evidence_items: int
    supported_evidence_items: int
    citation_presence_correct: bool

    @property
    def fact_coverage(self) -> float | None:
        if not self.expected_facts:
            return None
        return self.matched_facts / self.expected_facts

    @property
    def menu_path_coverage(self) -> float | None:
        if not self.expected_menu_paths:
            return None
        return self.matched_menu_paths / self.expected_menu_paths

    @property
    def citation_relevance(self) -> float | None:
        if not self.total_citations:
            return None
        return self.relevant_citations / self.total_citations

    @property
    def citation_expected_coverage(self) -> float | None:
        if not self.expected_citation_groups:
            return None
        return self.matched_citation_groups / self.expected_citation_groups

    @property
    def citation_evidence_coverage(self) -> float | None:
        if not self.matched_evidence_items:
            return None
        return self.supported_evidence_items / self.matched_evidence_items


def _strings(value: object, field: str, case_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise BenchmarkError(f"{case_id}: {field} doit être une liste de chaînes non vides")
    return tuple(item.strip() for item in value)


def _string_groups(value: object, field: str, case_id: str) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise BenchmarkError(f"{case_id}: {field} doit être une liste")
    groups: list[tuple[str, ...]] = []
    for index, group in enumerate(value, start=1):
        if not isinstance(group, list) or not group or not all(
            isinstance(item, str) and item.strip() for item in group
        ):
            raise BenchmarkError(
                f"{case_id}: {field}[{index}] doit être une liste non vide de chaînes"
            )
        groups.append(tuple(item.strip() for item in group))
    return tuple(groups)


def _groups(value: object, case_id: str) -> tuple[tuple[str, ...], ...]:
    return _string_groups(value, "relevant_section_groups", case_id)


def _history_messages(value: object, case_id: str) -> tuple[dict[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise BenchmarkError(f"{case_id}: history doit être une liste")
    messages: list[dict[str, str]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise BenchmarkError(f"{case_id}: history[{index}] doit être un objet")
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"}:
            raise BenchmarkError(
                f"{case_id}: history[{index}].role doit valoir user ou assistant"
            )
        if not content:
            raise BenchmarkError(f"{case_id}: history[{index}].content est vide")
        messages.append({"role": role, "content": content})
    return tuple(messages)


def _case_from_dict(item: dict, line_number: int) -> BenchmarkCase:
    case_id = str(item.get("id") or f"line-{line_number}").strip()
    question = str(item.get("question") or "").strip()
    category = str(item.get("category") or "uncategorized").strip()
    answerability = item.get("answerability")
    if answerability is None:
        answerability = "full" if item.get("answerable", True) else "none"
    answerability = str(answerability).strip().lower()

    groups = _groups(item.get("relevant_section_groups"), case_id)
    legacy_keywords = item.get("expected_section_keywords")
    if not groups and legacy_keywords:
        groups = (_strings(legacy_keywords, "expected_section_keywords", case_id),)

    if not question:
        raise BenchmarkError(f"{case_id}: question vide")
    if not category:
        raise BenchmarkError(f"{case_id}: catégorie vide")
    if answerability not in ANSWERABILITY_VALUES:
        raise BenchmarkError(
            f"{case_id}: answerability doit valoir full, partial ou none (reçu: {answerability})"
        )
    if answerability != "none" and not groups:
        raise BenchmarkError(f"{case_id}: au moins un relevant_section_group est requis")
    if answerability == "none" and groups:
        raise BenchmarkError(f"{case_id}: une question non répondable ne doit pas déclarer de section attendue")

    expected_menu_paths = _strings(item.get("expected_menu_paths"), "expected_menu_paths", case_id)
    expected_menu_path_groups = _string_groups(
        item.get("expected_menu_path_groups"), "expected_menu_path_groups", case_id
    )
    if expected_menu_paths and expected_menu_path_groups:
        raise BenchmarkError(
            f"{case_id}: utiliser expected_menu_paths ou expected_menu_path_groups, pas les deux"
        )

    required_facts = _strings(item.get("required_facts"), "required_facts", case_id)
    required_fact_groups = _string_groups(
        item.get("required_fact_groups"), "required_fact_groups", case_id
    )
    if required_facts and required_fact_groups:
        raise BenchmarkError(
            f"{case_id}: utiliser required_facts ou required_fact_groups, pas les deux"
        )

    return BenchmarkCase(
        case_id=case_id,
        category=category,
        question=question,
        answerability=answerability,
        relevant_section_groups=groups,
        expected_citation_groups=_string_groups(
            item.get("expected_citation_groups"), "expected_citation_groups", case_id
        ),
        expected_heading_paths=_strings(
            item.get("expected_heading_paths"), "expected_heading_paths", case_id
        ),
        expected_menu_paths=expected_menu_paths,
        expected_menu_path_groups=expected_menu_path_groups,
        required_facts=required_facts,
        required_fact_groups=required_fact_groups,
        history=_history_messages(item.get("history"), case_id),
        notes=str(item.get("notes") or "").strip(),
    )


def load_benchmark(path: Path) -> list[BenchmarkCase]:
    cases: list[BenchmarkCase] = []
    seen_ids: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise BenchmarkError(f"{path}:{line_number}: JSON invalide: {exc.msg}") from exc
            if not isinstance(item, dict):
                raise BenchmarkError(f"{path}:{line_number}: chaque ligne doit être un objet JSON")
            case = _case_from_dict(item, line_number)
            if case.case_id in seen_ids:
                raise BenchmarkError(f"{path}:{line_number}: id dupliqué: {case.case_id}")
            seen_ids.add(case.case_id)
            cases.append(case)
    if not cases:
        raise BenchmarkError(f"Benchmark vide : {path}")
    return cases


def _normalized_section(section: RetrievedSection) -> str:
    return normalize_domain_text(f"{section.title} {section.heading_path} {section.source_text}")


def _matches_group(section: RetrievedSection, group: tuple[str, ...]) -> bool:
    haystack = _normalized_section(section)
    return all(normalize_domain_text(keyword) in haystack for keyword in group)


def evaluate_retrieval_case(
    case: BenchmarkCase,
    limit: int,
    *,
    db_path: Path | None = None,
) -> RetrievalCaseResult:
    started = time.perf_counter()
    retrieval_question = _retrieval_question(case.question, list(case.history))
    rows = search_sections(retrieval_question, limit=limit, db_path=db_path)
    latency_ms = (time.perf_counter() - started) * 1000.0

    if case.answerability == "none":
        return RetrievalCaseResult(
            case=case,
            first_relevant_rank=None,
            matched_groups=0,
            expected_groups=0,
            latency_ms=latency_ms,
        )

    matched_group_indexes: set[int] = set()
    first_rank: int | None = None
    for rank, row in enumerate(rows, start=1):
        row_matches = False
        for index, group in enumerate(case.relevant_section_groups):
            if _matches_group(row, group):
                matched_group_indexes.add(index)
                row_matches = True
        if row_matches and first_rank is None:
            first_rank = rank

    return RetrievalCaseResult(
        case=case,
        first_relevant_rank=first_rank,
        matched_groups=len(matched_group_indexes),
        expected_groups=len(case.relevant_section_groups),
        latency_ms=latency_ms,
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def run_retrieval_benchmark(
    cases: list[BenchmarkCase],
    limit: int,
    *,
    db_path: Path | None = None,
) -> tuple[list[RetrievalCaseResult], dict]:
    results = [
        evaluate_retrieval_case(case, limit, db_path=db_path)
        for case in cases
    ]
    return results, summarize_retrieval(results, limit)


def summarize_retrieval(results: list[RetrievalCaseResult], limit: int) -> dict:
    answerable = [result for result in results if result.case.answerability != "none"]
    hits = sum(result.hit for result in answerable)
    expected_groups = sum(result.expected_groups for result in answerable)
    matched_groups = sum(result.matched_groups for result in answerable)
    latencies = [result.latency_ms for result in results]

    categories: dict[str, dict[str, float | int]] = {}
    for category in sorted({result.case.category for result in results}):
        scoped = [
            result
            for result in answerable
            if result.case.category == category
        ]
        if not scoped:
            continue
        category_hits = sum(result.hit for result in scoped)
        categories[category] = {
            "cases": len(scoped),
            "recall_at_k": category_hits / len(scoped),
            "mrr": sum(result.reciprocal_rank for result in scoped) / len(scoped),
            "group_coverage": (
                sum(result.matched_groups for result in scoped)
                / sum(result.expected_groups for result in scoped)
            ),
        }

    return {
        "limit": limit,
        "cases": len(results),
        "answerable_cases": len(answerable),
        "unanswerable_cases": len(results) - len(answerable),
        "recall_at_k": hits / len(answerable) if answerable else 0.0,
        "mrr": (
            sum(result.reciprocal_rank for result in answerable) / len(answerable)
            if answerable
            else 0.0
        ),
        "group_coverage": matched_groups / expected_groups if expected_groups else 0.0,
        "latency_mean_ms": sum(latencies) / len(latencies) if latencies else 0.0,
        "latency_p95_ms": _percentile(latencies, 0.95),
        "categories": categories,
    }


def retrieval_threshold_failures(
    summary: dict,
    *,
    min_recall: float | None = None,
    min_mrr: float | None = None,
    min_group_coverage: float | None = None,
) -> list[str]:
    thresholds = {
        "Recall": min_recall,
        "MRR": min_mrr,
        "couverture": min_group_coverage,
    }
    for label, threshold in thresholds.items():
        if threshold is not None and not 0.0 <= threshold <= 1.0:
            raise BenchmarkError(f"Le seuil {label} doit être compris entre 0 et 1.")

    failures: list[str] = []
    if min_recall is not None and summary["recall_at_k"] < min_recall:
        failures.append(f"Recall {summary['recall_at_k']:.3f} < {min_recall:.3f}")
    if min_mrr is not None and summary["mrr"] < min_mrr:
        failures.append(f"MRR {summary['mrr']:.3f} < {min_mrr:.3f}")
    if min_group_coverage is not None and summary["group_coverage"] < min_group_coverage:
        failures.append(
            f"couverture {summary['group_coverage']:.3f} < {min_group_coverage:.3f}"
        )
    return failures


async def generate_answer_for_case(
    case: BenchmarkCase,
    limit: int = 6,
    *,
    db_path: Path | None = None,
) -> GeneratedAnswer:
    total_started = time.perf_counter()
    retrieval_started = time.perf_counter()
    history = list(case.history)
    retrieval_question = _retrieval_question(case.question, history)
    sections = search_sections(retrieval_question, limit=limit, db_path=db_path)
    retrieval_latency_ms = (time.perf_counter() - retrieval_started) * 1000.0

    if not sections:
        total_latency_ms = (time.perf_counter() - total_started) * 1000.0
        return GeneratedAnswer(
            text=ABSTENTION,
            citations=(),
            sections=(),
            retrieval_latency_ms=retrieval_latency_ms,
            generation_latency_ms=0.0,
            total_latency_ms=total_latency_ms,
            answer_status="none",
        )

    contexts = [
        SourceContext(
            source_id=f"S{index + 1}",
            title=section.title,
            heading_path=section.heading_path,
            text=section.source_text[: settings.source_char_limit],
            source_type=section.source_type,
            document_version=section.document_version,
            revision_date=section.revision_date,
        )
        for index, section in enumerate(sections)
    ]
    provider = get_provider()
    chunks: list[str] = []
    generation_started = time.perf_counter()
    async for chunk in provider.stream_answer(
        question=case.question,
        sources=contexts,
        history=_history_for_prompt(history),
    ):
        chunks.append(chunk)
        if sum(len(part) for part in chunks) > 40000:
            break
    generation_latency_ms = (time.perf_counter() - generation_started) * 1000.0

    answer, citation_rows = finalize_answer("".join(chunks), sections)
    citations = tuple(citation_rows)
    total_latency_ms = (time.perf_counter() - total_started) * 1000.0
    provider_metrics = getattr(provider, "last_metrics", None)
    metrics_payload = (
        provider_metrics.as_dict()
        if provider_metrics is not None and hasattr(provider_metrics, "as_dict")
        else None
    )
    provider_status = getattr(provider, "last_answer_status", None)
    answer_status = provider_status if provider_status in ANSWERABILITY_VALUES else None
    return GeneratedAnswer(
        text=answer,
        citations=citations,
        sections=tuple(sections),
        retrieval_latency_ms=retrieval_latency_ms,
        generation_latency_ms=generation_latency_ms,
        total_latency_ms=total_latency_ms,
        provider_metrics=metrics_payload,
        answer_status=answer_status,
    )


def _canonical_fact_token(token: str) -> str:
    if token.startswith(("imprim", "impress")):
        return "imprimer"
    if token.startswith("manuel"):
        return "manuel"
    if token.startswith("automati"):
        return "automatique"
    if token.startswith(("envoi", "envoy")):
        return "transmettre"
    if token.startswith(("transmi", "transmet")):
        return "transmettre"
    if token.endswith("ment") and len(token) > 7:
        token = token[:-4]
    if token.endswith("s") and len(token) > 4:
        token = token[:-1]
    return token


def _fact_tokens(value: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for token in normalize_domain_text(value).split():
        if token in FACT_STOPWORDS or len(token) < 2:
            continue
        token = _canonical_fact_token(token)
        if token and token not in tokens:
            tokens.append(token)
    return tuple(tokens)


def _contains_expected(text: str, expected: str) -> bool:
    normalized_text = normalize_domain_text(text)
    normalized_expected = normalize_domain_text(expected)
    if normalized_expected in normalized_text:
        return True

    expected_tokens = _fact_tokens(expected)
    if len(expected_tokens) < 2:
        return False
    text_tokens = set(_fact_tokens(text))
    return all(token in text_tokens for token in expected_tokens)


def _effective_groups(
    legacy_values: tuple[str, ...],
    alternative_groups: tuple[tuple[str, ...], ...],
) -> tuple[tuple[str, ...], ...]:
    if alternative_groups:
        return alternative_groups
    return tuple((value,) for value in legacy_values)


def _contains_menu_path(text: str, expected: str) -> bool:
    haystack = normalize_domain_text(text)
    raw_segments = re.split(r"\s*(?:>|→|\+)\s*", expected)
    segments = [normalize_domain_text(segment) for segment in raw_segments if normalize_domain_text(segment)]
    if len(segments) <= 1:
        return _contains_expected(text, expected)

    cursor = 0
    for segment in segments:
        position = haystack.find(segment, cursor)
        if position < 0:
            return False
        cursor = position + len(segment)
    return True


def _is_abstention(text: str) -> bool:
    return normalize_domain_text(text) == normalize_domain_text(ABSTENTION)


def _section_evidence_text(section: RetrievedSection) -> str:
    return f"{section.title}\n{section.heading_path}\n{section.source_text}"


def _evidence_supported_by_citation(
    sections: tuple[RetrievedSection, ...],
    alternatives: tuple[str, ...],
    matcher,
) -> bool:
    return any(
        any(matcher(_section_evidence_text(section), alternative) for alternative in alternatives)
        for section in sections
    )


def _menu_evidence_supported_by_citations(
    sections: tuple[RetrievedSection, ...],
    alternatives: tuple[str, ...],
) -> bool:
    evidence = "\n".join(_section_evidence_text(section) for section in sections)
    for alternative in alternatives:
        raw_segments = re.split(r"\s*(?:>|→|\+)\s*", alternative)
        segments = [segment for segment in raw_segments if normalize_domain_text(segment)]
        if segments and all(_contains_expected(evidence, segment) for segment in segments):
            return True
    return False


def score_answer_case(case: BenchmarkCase, answer: GeneratedAnswer) -> AnswerCaseResult:
    is_abstention = _is_abstention(answer.text)
    abstention_correct = is_abstention if case.answerability == "none" else not is_abstention
    answer_status_correct = (
        None if answer.answer_status is None else answer.answer_status == case.answerability
    )

    fact_groups = _effective_groups(case.required_facts, case.required_fact_groups)
    menu_path_groups = _effective_groups(case.expected_menu_paths, case.expected_menu_path_groups)
    matched_fact_groups = tuple(
        alternatives
        for alternatives in fact_groups
        if any(_contains_expected(answer.text, alternative) for alternative in alternatives)
    )
    matched_menu_path_groups = tuple(
        alternatives
        for alternatives in menu_path_groups
        if any(_contains_menu_path(answer.text, alternative) for alternative in alternatives)
    )
    matched_facts = len(matched_fact_groups)
    matched_menu_paths = len(matched_menu_path_groups)

    citation_groups = case.expected_citation_groups or case.relevant_section_groups
    relevant_section_ids = {
        section.id
        for section in answer.sections
        if any(_matches_group(section, group) for group in citation_groups)
    }
    cited_section_ids = {
        int(citation["section_id"])
        for citation in answer.citations
        if "section_id" in citation
    }
    relevant_citations = len(cited_section_ids & relevant_section_ids)
    total_citations = len(cited_section_ids)
    matched_citation_groups = sum(
        any(section.id in cited_section_ids and _matches_group(section, group) for section in answer.sections)
        for group in citation_groups
    )
    cited_sections = tuple(section for section in answer.sections if section.id in cited_section_ids)
    supported_fact_items = sum(
        _evidence_supported_by_citation(cited_sections, alternatives, _contains_expected)
        for alternatives in matched_fact_groups
    )
    supported_menu_items = sum(
        _menu_evidence_supported_by_citations(cited_sections, alternatives)
        for alternatives in matched_menu_path_groups
    )
    matched_evidence_items = len(matched_fact_groups) + len(matched_menu_path_groups)
    supported_evidence_items = supported_fact_items + supported_menu_items
    citation_presence_correct = (
        total_citations == 0 if case.answerability == "none" else total_citations > 0
    )

    return AnswerCaseResult(
        case=case,
        answer=answer,
        abstention_correct=abstention_correct,
        answer_status_correct=answer_status_correct,
        matched_facts=matched_facts,
        expected_facts=len(fact_groups),
        matched_menu_paths=matched_menu_paths,
        expected_menu_paths=len(menu_path_groups),
        relevant_citations=relevant_citations,
        total_citations=total_citations,
        matched_citation_groups=matched_citation_groups,
        expected_citation_groups=len(citation_groups),
        matched_evidence_items=matched_evidence_items,
        supported_evidence_items=supported_evidence_items,
        citation_presence_correct=citation_presence_correct,
    )


def answer_quality_failures(
    results: list[AnswerCaseResult],
    *,
    require_status: bool = False,
) -> list[AnswerCaseResult]:
    failures: list[AnswerCaseResult] = []
    for result in results:
        status_failed = (
            result.answer_status_correct is not True
            if require_status
            else result.answer_status_correct is False
        )
        if (
            not result.abstention_correct
            or status_failed
            or (result.expected_facts and result.matched_facts < result.expected_facts)
            or (
                result.expected_menu_paths
                and result.matched_menu_paths < result.expected_menu_paths
            )
            or not result.citation_presence_correct
            or (
                result.expected_citation_groups
                and result.matched_citation_groups < result.expected_citation_groups
            )
            or (
                result.matched_evidence_items
                and result.supported_evidence_items < result.matched_evidence_items
            )
        ):
            failures.append(result)
    return failures


def answer_quality_failure_reasons(result: AnswerCaseResult) -> tuple[str, ...]:
    failures: list[str] = []
    if not result.abstention_correct:
        failures.append("abstention")
    if result.answer_status_correct is not True:
        failures.append("answer_status")
    if result.expected_facts and result.matched_facts < result.expected_facts:
        failures.append("required_facts")
    if result.expected_menu_paths and result.matched_menu_paths < result.expected_menu_paths:
        failures.append("menu_paths")
    if not result.citation_presence_correct:
        failures.append("citation_presence")
    if (
        result.expected_citation_groups
        and result.matched_citation_groups < result.expected_citation_groups
    ):
        failures.append("citation_expected_coverage")
    if (
        result.matched_evidence_items
        and result.supported_evidence_items < result.matched_evidence_items
    ):
        failures.append("citation_evidence")
    return tuple(failures)


def answer_result_to_dict(result: AnswerCaseResult) -> dict:
    return {
        "id": result.case.case_id,
        "category": result.case.category,
        "question": result.case.question,
        "answerability": result.case.answerability,
        "history": list(result.case.history),
        "answer": result.answer.text,
        "answer_status": result.answer.answer_status,
        "abstention_correct": result.abstention_correct,
        "answer_status_correct": result.answer_status_correct,
        "matched_facts": result.matched_facts,
        "expected_facts": result.expected_facts,
        "matched_menu_paths": result.matched_menu_paths,
        "expected_menu_paths": result.expected_menu_paths,
        "relevant_citations": result.relevant_citations,
        "total_citations": result.total_citations,
        "matched_citation_groups": result.matched_citation_groups,
        "expected_citation_groups": result.expected_citation_groups,
        "matched_evidence_items": result.matched_evidence_items,
        "supported_evidence_items": result.supported_evidence_items,
        "citation_evidence_coverage": result.citation_evidence_coverage,
        "citation_presence_correct": result.citation_presence_correct,
        "retrieval_latency_ms": result.answer.retrieval_latency_ms,
        "generation_latency_ms": result.answer.generation_latency_ms,
        "total_latency_ms": result.answer.total_latency_ms,
        "provider_metrics": result.answer.provider_metrics,
        "citations": list(result.answer.citations),
        "retrieved_sections": [
            {
                "section_id": section.id,
                "title": section.title,
                "heading_path": section.heading_path,
                "source_url": section.source_url,
                "document_name": section.document_name,
                "document_version": section.document_version,
                "revision_date": section.revision_date,
                "document_hash": section.document_hash,
                "score": section.score,
            }
            for section in result.answer.sections
        ],
    }


def summarize_answers(results: list[AnswerCaseResult]) -> dict:
    fact_total = sum(result.expected_facts for result in results)
    fact_matches = sum(result.matched_facts for result in results)
    menu_total = sum(result.expected_menu_paths for result in results)
    menu_matches = sum(result.matched_menu_paths for result in results)
    cited_total = sum(result.total_citations for result in results)
    relevant_citations = sum(result.relevant_citations for result in results)
    citation_group_total = sum(result.expected_citation_groups for result in results)
    citation_group_matches = sum(result.matched_citation_groups for result in results)
    matched_evidence_items = sum(result.matched_evidence_items for result in results)
    supported_evidence_items = sum(result.supported_evidence_items for result in results)
    status_results = [
        result.answer_status_correct
        for result in results
        if result.answer_status_correct is not None
    ]
    status_matches = sum(item is True for item in status_results)
    latencies = [result.answer.total_latency_ms for result in results]
    provider_metrics = [
        result.answer.provider_metrics
        for result in results
        if isinstance(result.answer.provider_metrics, dict)
    ]
    provider_usage = None
    if provider_metrics:
        provider_usage = {
            "cases": len(provider_metrics),
            "input_tokens_total": sum(int(item.get("input_tokens") or 0) for item in provider_metrics),
            "cached_input_tokens_total": sum(
                int(item.get("cached_input_tokens") or 0) for item in provider_metrics
            ),
            "output_tokens_total": sum(int(item.get("output_tokens") or 0) for item in provider_metrics),
            "reasoning_output_tokens_total": sum(
                int(item.get("reasoning_output_tokens") or 0) for item in provider_metrics
            ),
        }

    return {
        "cases": len(results),
        "abstention_accuracy": (
            sum(result.abstention_correct for result in results) / len(results)
            if results
            else 0.0
        ),
        "answer_status_accuracy": (
            status_matches / len(status_results) if status_results else None
        ),
        "answer_status_matches": status_matches,
        "answer_status_total": len(status_results),
        "fact_coverage": fact_matches / fact_total if fact_total else None,
        "fact_matches": fact_matches,
        "fact_total": fact_total,
        "menu_path_coverage": menu_matches / menu_total if menu_total else None,
        "menu_matches": menu_matches,
        "menu_total": menu_total,
        "citation_presence_accuracy": (
            sum(result.citation_presence_correct for result in results) / len(results)
            if results
            else 0.0
        ),
        "citation_relevance": relevant_citations / cited_total if cited_total else None,
        "citation_expected_coverage": (
            citation_group_matches / citation_group_total if citation_group_total else None
        ),
        "matched_citation_groups": citation_group_matches,
        "expected_citation_groups": citation_group_total,
        "citation_evidence_coverage": (
            supported_evidence_items / matched_evidence_items if matched_evidence_items else None
        ),
        "matched_evidence_items": matched_evidence_items,
        "supported_evidence_items": supported_evidence_items,
        "relevant_citations": relevant_citations,
        "citations": cited_total,
        "latency_mean_ms": sum(latencies) / len(latencies) if latencies else 0.0,
        "latency_p95_ms": _percentile(latencies, 0.95),
        "provider_usage": provider_usage,
    }


def _section_from_report_payload(
    conn: sqlite3.Connection,
    payload: dict,
) -> RetrievedSection:
    section_id = int(payload["section_id"])
    document_hash = str(payload["document_hash"])
    source_url = str(payload["source_url"])
    heading_path = str(payload["heading_path"])
    rows = conn.execute(
        """
        SELECT s.source_text
        FROM sections s
        JOIN documents d ON d.id = s.document_id
        WHERE d.content_hash = ? AND s.source_url = ? AND s.heading_path = ?
        LIMIT 2
        """,
        (document_hash, source_url, heading_path),
    ).fetchall()
    if len(rows) != 1:
        raise BenchmarkError(
            "Section introuvable ou ambiguë dans docs.db lors du re-scoring : "
            f"hash={document_hash} url={source_url} path={heading_path}"
        )
    row = rows[0]
    return RetrievedSection(
        id=section_id,
        title=str(payload["title"]),
        heading_path=str(payload["heading_path"]),
        source_url=str(payload["source_url"]),
        source_text=str(row["source_text"]),
        document_name=str(payload["document_name"]),
        document_version=payload.get("document_version"),
        revision_date=payload.get("revision_date"),
        document_hash=str(payload["document_hash"]),
        score=float(payload["score"]),
    )


def rescore_answer_report(
    benchmark_path: Path,
    report_path: Path,
    docs_db_path: Path,
) -> tuple[list[AnswerCaseResult], dict]:
    cases = load_benchmark(benchmark_path)
    by_question = {case.question: case for case in cases}
    if len(by_question) != len(cases):
        raise BenchmarkError("Le benchmark contient des questions dupliquées ; re-scoring ambigu.")

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"Rapport illisible : {report_path}: {exc}") from exc
    report_cases = report.get("cases")
    if not isinstance(report_cases, list):
        raise BenchmarkError(f"{report_path}: liste 'cases' absente du rapport")

    results: list[AnswerCaseResult] = []
    try:
        with sqlite3.connect(docs_db_path) as conn:
            conn.row_factory = sqlite3.Row
            for payload in report_cases:
                if not isinstance(payload, dict):
                    raise BenchmarkError(f"{report_path}: cas de rapport invalide")
                question = str(payload.get("question") or "").strip()
                case = by_question.get(question)
                if case is None:
                    raise BenchmarkError(
                        f"Question du rapport absente du benchmark courant : {question}"
                    )
                retrieved = payload.get("retrieved_sections", [])
                if not isinstance(retrieved, list):
                    raise BenchmarkError(f"{case.case_id}: retrieved_sections invalide")
                sections = tuple(
                    _section_from_report_payload(conn, section)
                    for section in retrieved
                    if isinstance(section, dict)
                )
                citations = payload.get("citations", [])
                if not isinstance(citations, list):
                    raise BenchmarkError(f"{case.case_id}: citations invalides")
                raw_answer_status = payload.get("answer_status")
                answer_status = (
                    str(raw_answer_status)
                    if raw_answer_status in ANSWERABILITY_VALUES
                    else None
                )
                answer = GeneratedAnswer(
                    text=str(payload.get("answer") or ""),
                    citations=tuple(citation for citation in citations if isinstance(citation, dict)),
                    sections=sections,
                    retrieval_latency_ms=float(payload.get("retrieval_latency_ms", 0.0)),
                    generation_latency_ms=float(payload.get("generation_latency_ms", 0.0)),
                    total_latency_ms=float(payload.get("total_latency_ms", 0.0)),
                    provider_metrics=(
                        payload.get("provider_metrics")
                        if isinstance(payload.get("provider_metrics"), dict)
                        else None
                    ),
                    answer_status=answer_status,
                )
                results.append(score_answer_case(case, answer))
    except sqlite3.Error as exc:
        raise BenchmarkError(f"Impossible de relire {docs_db_path}: {exc}") from exc

    return results, summarize_answers(results)
