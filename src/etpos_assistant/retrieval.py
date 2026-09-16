from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace

from .db import docs_db
from .vocabulary import RetrievalConcept, detect_retrieval_concepts, normalize_domain_text

STOPWORDS = {
    "a", "au", "aux", "avec", "ce", "ces", "comment", "dans", "de", "des", "du",
    "elle", "en", "et", "est", "faire", "il", "je", "la", "le", "les", "ma", "mes",
    "mon", "ne", "ou", "par", "pas", "pour", "que", "quel", "quelle", "qui", "sa",
    "se", "ses", "son", "sur", "un", "une", "vous", "votre", "vos",
}

RRF_K = 30
CONCEPT_QUERY_WEIGHT = 1.4


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
    score: float


@dataclass(frozen=True)
class SearchVariant:
    label: str
    query: str
    weight: float


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


def build_search_plan(question: str) -> list[SearchVariant]:
    base_query = build_fts_query(question)
    if not base_query:
        return []

    variants = [SearchVariant(label="lexical", query=base_query, weight=1.0)]
    seen = {base_query}
    for concept in detect_retrieval_concepts(question):
        for index, terms in enumerate(concept.query_variants, start=1):
            query = _build_concept_query(terms)
            if not query or query in seen:
                continue
            seen.add(query)
            variants.append(
                SearchVariant(
                    label=f"{concept.key}:{index}",
                    query=query,
                    weight=CONCEPT_QUERY_WEIGHT,
                )
            )
    return variants


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
        score=float(row["score"]),
    )


def _query_sections(conn, query: str, limit: int) -> list[RetrievedSection]:
    rows = conn.execute(
        """
        SELECT s.id, s.title, s.heading_path, s.source_url, s.source_text,
               d.name AS document_name, d.detected_version, d.detected_revision_date,
               bm25(sections_fts, 8.0, 5.0, 1.0) AS score
        FROM sections_fts
        JOIN sections s ON s.id = sections_fts.rowid
        JOIN documents d ON d.id = s.document_id
        WHERE sections_fts MATCH ?
        ORDER BY score ASC, d.source_priority DESC, s.section_order ASC
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()
    return [_row_to_section(row) for row in rows]


def _signal_score(section: RetrievedSection, concepts: tuple[RetrievalConcept, ...]) -> float:
    path = normalize_domain_text(section.heading_path)
    content = normalize_domain_text(f"{section.title} {section.heading_path} {section.source_text}")
    score = 0.0

    for concept in concepts:
        for signal in concept.path_signals:
            if normalize_domain_text(signal) in path:
                score += 0.020
        for signal in concept.text_signals:
            if normalize_domain_text(signal) in content:
                score += 0.010
        for signal in concept.negative_signals:
            if normalize_domain_text(signal) in content:
                score -= 0.025
    return score


def search_sections(question: str, limit: int = 6) -> list[RetrievedSection]:
    plan = build_search_plan(question)
    if not plan:
        return []

    concepts = detect_retrieval_concepts(question)
    with docs_db() as conn:
        if len(plan) == 1:
            return _query_sections(conn, plan[0].query, limit)

        candidate_limit = max(48, limit * 8)
        candidates: dict[int, dict[str, object]] = {}
        for variant in plan:
            for rank, section in enumerate(_query_sections(conn, variant.query, candidate_limit), start=1):
                candidate = candidates.setdefault(
                    section.id,
                    {
                        "section": section,
                        "rrf": 0.0,
                        "best_bm25": section.score,
                    },
                )
                candidate["rrf"] = float(candidate["rrf"]) + variant.weight / (RRF_K + rank)
                candidate["best_bm25"] = min(float(candidate["best_bm25"]), section.score)

    ranked: list[tuple[RetrievedSection, float]] = []
    for candidate in candidates.values():
        section = candidate["section"]
        assert isinstance(section, RetrievedSection)
        fused_score = float(candidate["rrf"]) + _signal_score(section, concepts)
        ranked.append((replace(section, score=-fused_score), float(candidate["best_bm25"])))

    ranked.sort(key=lambda item: (item[0].score, item[1], item[0].id))
    return [section for section, _ in ranked[:limit]]
