from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from .db import docs_db
from .vocabulary import QueryAnalysis, RetrievalConcept, analyze_query, normalize_domain_text

STOPWORDS = {
    "a", "au", "aux", "avec", "ce", "ces", "comment", "dans", "de", "des", "du",
    "elle", "en", "et", "est", "faire", "il", "je", "la", "le", "les", "ma", "mes",
    "mon", "ne", "ou", "par", "pas", "pour", "que", "quel", "quelle", "qui", "sa",
    "se", "ses", "son", "sur", "un", "une", "vous", "votre", "vos",
}


@dataclass(frozen=True)
class RetrievalTuning:
    title_weight: float = 8.0
    heading_path_weight: float = 5.0
    body_weight: float = 1.0
    rrf_k: int = 30
    concept_query_weight: float = 1.4
    candidate_limit_min: int = 48
    candidate_limit_multiplier: int = 8
    path_signal_weight: float = 0.020
    text_signal_weight: float = 0.010
    negative_signal_weight: float = 0.025


DEFAULT_TUNING = RetrievalTuning()


@dataclass(frozen=True)
class RetrievedSection:
    id: int
    title: str
    heading_path: str
    source_url: str
    source_text: str
    document_name: str
    document_version: str | None
    revision_date: str | None
    document_hash: str
    score: float


@dataclass(frozen=True)
class SearchVariant:
    label: str
    query: str
    weight: float


@dataclass(frozen=True)
class VariantHitTrace:
    section_id: int
    title: str
    heading_path: str
    rank: int
    bm25_score: float


@dataclass(frozen=True)
class VariantTrace:
    label: str
    query: str
    weight: float
    hits: tuple[VariantHitTrace, ...]


@dataclass(frozen=True)
class CandidateTrace:
    section_id: int
    title: str
    heading_path: str
    rrf_score: float
    signal_score: float
    best_bm25_score: float
    final_score: float
    matched_variants: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalTrace:
    analysis: QueryAnalysis
    variants: tuple[VariantTrace, ...]
    candidates: tuple[CandidateTrace, ...]


def normalize_search_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _search_tokens(value: str) -> list[str]:
    return re.findall(r"[\wÀ-ÿ]+", normalize_search_text(value).lower(), flags=re.UNICODE)


def build_fts_query(question: str) -> str:
    selected: list[str] = []
    for token in _search_tokens(question):
        if len(token) < 2 or token in STOPWORDS:
            continue
        if token not in selected:
            selected.append(token)
        if len(selected) >= 12:
            break
    return " OR ".join(f'"{token}"*' for token in selected)


def _build_concept_query(terms: tuple[str, ...]) -> str:
    selected: list[str] = []
    for term in terms:
        for token in _search_tokens(term):
            if len(token) < 2:
                continue
            if token not in selected:
                selected.append(token)
    return " AND ".join(f'"{token}"*' for token in selected)


def _build_search_plan(
    question: str,
    analysis: QueryAnalysis,
    tuning: RetrievalTuning,
) -> list[SearchVariant]:
    base_query = build_fts_query(question)
    if not base_query:
        return []

    variants = [SearchVariant(label="lexical", query=base_query, weight=1.0)]
    seen = {base_query}
    for concept in analysis.concepts:
        for index, terms in enumerate(concept.query_variants, start=1):
            query = _build_concept_query(terms)
            if not query or query in seen:
                continue
            seen.add(query)
            variants.append(
                SearchVariant(
                    label=f"{concept.key}:{index}",
                    query=query,
                    weight=tuning.concept_query_weight,
                )
            )
    return variants


def build_search_plan(
    question: str,
    tuning: RetrievalTuning = DEFAULT_TUNING,
) -> list[SearchVariant]:
    return _build_search_plan(question, analyze_query(question), tuning)


def _row_to_section(row) -> RetrievedSection:
    return RetrievedSection(
        id=row["id"],
        title=row["title"],
        heading_path=row["heading_path"],
        source_url=row["source_url"],
        source_text=row["source_text"],
        document_name=row["document_name"],
        document_version=row["detected_version"],
        revision_date=row["detected_revision_date"],
        document_hash=row["content_hash"],
        score=float(row["score"]),
    )


def _query_sections(
    conn,
    query: str,
    limit: int,
    tuning: RetrievalTuning,
) -> list[RetrievedSection]:
    sql = f"""
        SELECT s.id, s.title, s.heading_path, s.source_url, s.source_text,
               d.name AS document_name, d.detected_version, d.detected_revision_date, d.content_hash,
               bm25(
                   sections_fts,
                   {float(tuning.title_weight)},
                   {float(tuning.heading_path_weight)},
                   {float(tuning.body_weight)}
               ) AS score
        FROM sections_fts
        JOIN sections s ON s.id = sections_fts.rowid
        JOIN documents d ON d.id = s.document_id
        WHERE sections_fts MATCH ?
        ORDER BY score ASC, d.source_priority DESC, s.section_order ASC
        LIMIT ?
    """
    rows = conn.execute(sql, (query, limit)).fetchall()
    return [_row_to_section(row) for row in rows]


def _signal_score(
    section: RetrievedSection,
    concepts: tuple[RetrievalConcept, ...],
    tuning: RetrievalTuning,
) -> float:
    path = normalize_domain_text(section.heading_path)
    content = normalize_domain_text(f"{section.title} {section.heading_path} {section.source_text}")
    score = 0.0

    for concept in concepts:
        for signal in concept.path_signals:
            if normalize_domain_text(signal) in path:
                score += tuning.path_signal_weight
        for signal in concept.text_signals:
            if normalize_domain_text(signal) in content:
                score += tuning.text_signal_weight
        for signal in concept.negative_signals:
            if normalize_domain_text(signal) in content:
                score -= tuning.negative_signal_weight
    return score


def _variant_trace(variant: SearchVariant, rows: list[RetrievedSection]) -> VariantTrace:
    return VariantTrace(
        label=variant.label,
        query=variant.query,
        weight=variant.weight,
        hits=tuple(
            VariantHitTrace(
                section_id=section.id,
                title=section.title,
                heading_path=section.heading_path,
                rank=rank,
                bm25_score=section.score,
            )
            for rank, section in enumerate(rows, start=1)
        ),
    )


def search_sections_with_trace(
    question: str,
    limit: int = 6,
    tuning: RetrievalTuning = DEFAULT_TUNING,
    db_path: Path | None = None,
) -> tuple[list[RetrievedSection], RetrievalTrace]:
    analysis = analyze_query(question)
    plan = _build_search_plan(question, analysis, tuning)
    if not plan:
        return [], RetrievalTrace(analysis=analysis, variants=(), candidates=())

    variant_traces: list[VariantTrace] = []
    with docs_db(db_path) as conn:
        if len(plan) == 1:
            rows = _query_sections(conn, plan[0].query, limit, tuning)
            variant_traces.append(_variant_trace(plan[0], rows))
            candidates = tuple(
                CandidateTrace(
                    section_id=section.id,
                    title=section.title,
                    heading_path=section.heading_path,
                    rrf_score=0.0,
                    signal_score=0.0,
                    best_bm25_score=section.score,
                    final_score=-section.score,
                    matched_variants=(plan[0].label,),
                )
                for section in rows
            )
            return rows, RetrievalTrace(
                analysis=analysis,
                variants=tuple(variant_traces),
                candidates=candidates,
            )

        candidate_limit = max(tuning.candidate_limit_min, limit * tuning.candidate_limit_multiplier)
        candidates: dict[int, dict[str, object]] = {}
        for variant in plan:
            rows = _query_sections(conn, variant.query, candidate_limit, tuning)
            variant_traces.append(_variant_trace(variant, rows))
            for rank, section in enumerate(rows, start=1):
                candidate = candidates.setdefault(
                    section.id,
                    {
                        "section": section,
                        "rrf": 0.0,
                        "best_bm25": section.score,
                        "variants": [],
                    },
                )
                candidate["rrf"] = float(candidate["rrf"]) + variant.weight / (tuning.rrf_k + rank)
                candidate["best_bm25"] = min(float(candidate["best_bm25"]), section.score)
                matched_variants = candidate["variants"]
                assert isinstance(matched_variants, list)
                matched_variants.append(variant.label)

    ranked: list[tuple[RetrievedSection, CandidateTrace]] = []
    for candidate in candidates.values():
        section = candidate["section"]
        assert isinstance(section, RetrievedSection)
        rrf_score = float(candidate["rrf"])
        signal_score = _signal_score(section, analysis.concepts, tuning)
        final_score = rrf_score + signal_score
        matched_variants = candidate["variants"]
        assert isinstance(matched_variants, list)
        trace = CandidateTrace(
            section_id=section.id,
            title=section.title,
            heading_path=section.heading_path,
            rrf_score=rrf_score,
            signal_score=signal_score,
            best_bm25_score=float(candidate["best_bm25"]),
            final_score=final_score,
            matched_variants=tuple(str(label) for label in matched_variants),
        )
        ranked.append((replace(section, score=-final_score), trace))

    ranked.sort(key=lambda item: (item[0].score, item[1].best_bm25_score, item[0].id))
    selected = [section for section, _ in ranked[:limit]]
    ordered_traces = tuple(trace for _, trace in ranked)
    return selected, RetrievalTrace(
        analysis=analysis,
        variants=tuple(variant_traces),
        candidates=ordered_traces,
    )


def search_sections(
    question: str,
    limit: int = 6,
    tuning: RetrievalTuning = DEFAULT_TUNING,
    db_path: Path | None = None,
) -> list[RetrievedSection]:
    rows, _ = search_sections_with_trace(
        question,
        limit=limit,
        tuning=tuning,
        db_path=db_path,
    )
    return rows
