from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

from .providers import SourceContext
from .rag import ABSTENTION, finalize_answer, get_provider
from .retrieval import RetrievedSection, search_sections
from .vocabulary import normalize_domain_text


ANSWERABILITY_VALUES = {"full", "partial", "none"}


class BenchmarkError(ValueError):
    pass


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    category: str
    question: str
    answerability: str
    relevant_section_groups: tuple[tuple[str, ...], ...]
    expected_heading_paths: tuple[str, ...] = ()
    expected_menu_paths: tuple[str, ...] = ()
    required_facts: tuple[str, ...] = ()
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


@dataclass(frozen=True)
class AnswerCaseResult:
    case: BenchmarkCase
    answer: GeneratedAnswer
    abstention_correct: bool
    matched_facts: int
    expected_facts: int
    matched_menu_paths: int
    expected_menu_paths: int
    relevant_citations: int
    total_citations: int
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


def _strings(value: object, field: str, case_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise BenchmarkError(f"{case_id}: {field} doit être une liste de chaînes non vides")
    return tuple(item.strip() for item in value)


def _groups(value: object, case_id: str) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise BenchmarkError(f"{case_id}: relevant_section_groups doit être une liste")
    groups: list[tuple[str, ...]] = []
    for index, group in enumerate(value, start=1):
        if not isinstance(group, list) or not group or not all(
            isinstance(item, str) and item.strip() for item in group
        ):
            raise BenchmarkError(
                f"{case_id}: relevant_section_groups[{index}] doit être une liste non vide de chaînes"
            )
        groups.append(tuple(item.strip() for item in group))
    return tuple(groups)


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

    return BenchmarkCase(
        case_id=case_id,
        category=category,
        question=question,
        answerability=answerability,
        relevant_section_groups=groups,
        expected_heading_paths=_strings(
            item.get("expected_heading_paths"), "expected_heading_paths", case_id
        ),
        expected_menu_paths=_strings(item.get("expected_menu_paths"), "expected_menu_paths", case_id),
        required_facts=_strings(item.get("required_facts"), "required_facts", case_id),
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
    rows = search_sections(case.question, limit=limit, db_path=db_path)
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
    sections = search_sections(case.question, limit=limit, db_path=db_path)
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
        )

    contexts = [
        SourceContext(
            source_id=f"S{index + 1}",
            title=section.title,
            heading_path=section.heading_path,
            text=section.source_text[:9000],
        )
        for index, section in enumerate(sections)
    ]
    provider = get_provider()
    chunks: list[str] = []
    generation_started = time.perf_counter()
    async for chunk in provider.stream_answer(
        question=case.question,
        sources=contexts,
        history=[],
    ):
        chunks.append(chunk)
        if sum(len(part) for part in chunks) > 40000:
            break
    generation_latency_ms = (time.perf_counter() - generation_started) * 1000.0

    answer, citation_rows = finalize_answer("".join(chunks), sections)
    citations = tuple(citation_rows)
    total_latency_ms = (time.perf_counter() - total_started) * 1000.0
    return GeneratedAnswer(
        text=answer,
        citations=citations,
        sections=tuple(sections),
        retrieval_latency_ms=retrieval_latency_ms,
        generation_latency_ms=generation_latency_ms,
        total_latency_ms=total_latency_ms,
    )


def _contains_expected(text: str, expected: str) -> bool:
    return normalize_domain_text(expected) in normalize_domain_text(text)


def _is_abstention(text: str) -> bool:
    return normalize_domain_text(text) == normalize_domain_text(ABSTENTION)


def score_answer_case(case: BenchmarkCase, answer: GeneratedAnswer) -> AnswerCaseResult:
    is_abstention = _is_abstention(answer.text)
    abstention_correct = is_abstention if case.answerability == "none" else not is_abstention

    matched_facts = sum(_contains_expected(answer.text, fact) for fact in case.required_facts)
    matched_menu_paths = sum(
        _contains_expected(answer.text, menu_path) for menu_path in case.expected_menu_paths
    )

    relevant_section_ids = {
        section.id
        for section in answer.sections
        if any(_matches_group(section, group) for group in case.relevant_section_groups)
    }
    cited_section_ids = {
        int(citation["section_id"])
        for citation in answer.citations
        if "section_id" in citation
    }
    relevant_citations = len(cited_section_ids & relevant_section_ids)
    total_citations = len(cited_section_ids)
    citation_presence_correct = (
        total_citations == 0 if case.answerability == "none" else total_citations > 0
    )

    return AnswerCaseResult(
        case=case,
        answer=answer,
        abstention_correct=abstention_correct,
        matched_facts=matched_facts,
        expected_facts=len(case.required_facts),
        matched_menu_paths=matched_menu_paths,
        expected_menu_paths=len(case.expected_menu_paths),
        relevant_citations=relevant_citations,
        total_citations=total_citations,
        citation_presence_correct=citation_presence_correct,
    )


def summarize_answers(results: list[AnswerCaseResult]) -> dict:
    fact_total = sum(result.expected_facts for result in results)
    fact_matches = sum(result.matched_facts for result in results)
    menu_total = sum(result.expected_menu_paths for result in results)
    menu_matches = sum(result.matched_menu_paths for result in results)
    cited_total = sum(result.total_citations for result in results)
    relevant_citations = sum(result.relevant_citations for result in results)
    latencies = [result.answer.total_latency_ms for result in results]

    return {
        "cases": len(results),
        "abstention_accuracy": (
            sum(result.abstention_correct for result in results) / len(results)
            if results
            else 0.0
        ),
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
        "relevant_citations": relevant_citations,
        "citations": cited_total,
        "latency_mean_ms": sum(latencies) / len(latencies) if latencies else 0.0,
        "latency_p95_ms": _percentile(latencies, 0.95),
    }
