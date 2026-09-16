from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import shutil
import subprocess
from pathlib import Path
from datetime import UTC, datetime

from .config import settings
from .db import app_db, docs_db, init_all
from .ingestion import ingest_enabled_sources
from .retrieval import search_sections, search_sections_with_trace
from .security import hash_password


def cmd_init_db(_args) -> None:
    init_all()
    print("Bases initialisées.")


def cmd_create_user(args) -> None:
    init_all()
    username = (args.username or input("Identifiant : ")).strip()
    if not username:
        raise SystemExit("Identifiant vide.")
    password = getpass.getpass("Mot de passe (12 caractères minimum) : ")
    if len(password) < 12:
        raise SystemExit("Mot de passe trop court.")
    confirm = getpass.getpass("Confirmer : ")
    if password != confirm:
        raise SystemExit("Les mots de passe diffèrent.")
    with app_db() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), datetime.now(UTC).isoformat()),
        )
    print(f"Utilisateur créé : {username}")


def cmd_ingest(_args) -> None:
    results = asyncio.run(ingest_enabled_sources())
    print(json.dumps(results, ensure_ascii=False, indent=2))


def cmd_search(args) -> None:
    init_all()
    if args.debug:
        rows, trace = search_sections_with_trace(args.query, limit=args.limit)
        analysis = trace.analysis
        print("Analyse de requête:")
        print(f"  normalisée: {analysis.normalized_question}")
        print(f"  intentions: {', '.join(analysis.intents) or '-'}")
        print(f"  objets: {', '.join(analysis.objects) or '-'}")
        print(f"  qualificatifs: {', '.join(analysis.qualifiers) or '-'}")
        print(f"  concepts: {', '.join(concept.key for concept in analysis.concepts) or '-'}")
        print("\nVariantes FTS:")
        for variant in trace.variants:
            print(f"  {variant.label} poids={variant.weight:.2f} -> {variant.query}")
            for hit in variant.hits[:5]:
                print(f"    {hit.rank}. bm25={hit.bm25_score:.4f} id={hit.section_id} {hit.heading_path}")
        if trace.candidates:
            print("\nFusion / reranking:")
            for index, candidate in enumerate(trace.candidates[: max(args.limit, 10)], start=1):
                print(
                    f"  {index}. final={candidate.final_score:.4f} rrf={candidate.rrf_score:.4f} "
                    f"signals={candidate.signal_score:+.4f} bm25={candidate.best_bm25_score:.4f} "
                    f"id={candidate.section_id} {candidate.heading_path} "
                    f"via={','.join(candidate.matched_variants)}"
                )
    else:
        rows = search_sections(args.query, limit=args.limit)

    if not rows:
        print("Aucun résultat.")
        return
    print("\nRésultats finaux:")
    for index, row in enumerate(rows, start=1):
        print(f"\n#{index} score={row.score:.4f} {row.heading_path}")
        print(row.source_url)
        print(" ".join(row.source_text.split())[:500])


def cmd_corpus_stats(_args) -> None:
    init_all()
    with docs_db() as conn:
        docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        sections = conn.execute("SELECT COUNT(*) AS n FROM sections").fetchone()["n"]
        rows = conn.execute(
            "SELECT name, detected_version, detected_revision_date, retrieved_at FROM documents ORDER BY source_priority DESC"
        ).fetchall()
    print(f"Documents : {docs}\nSections : {sections}")
    for row in rows:
        print(dict(row))



def _first_relevant_rank(rows, keywords: list[str]) -> int | None:
    if not keywords:
        return None
    for rank, row in enumerate(rows, start=1):
        haystack = f"{row.title} {row.heading_path} {row.source_text}".lower()
        if all(keyword in haystack for keyword in keywords):
            return rank
    return None


def cmd_eval_retrieval(args) -> None:
    init_all()
    path = args.path
    total = 0
    hits = 0
    reciprocal_rank_sum = 0.0
    for raw in open(path, encoding="utf-8"):
        raw = raw.strip()
        if not raw:
            continue
        item = json.loads(raw)
        if not item.get("answerable", True):
            continue
        keywords = [str(k).lower() for k in item.get("expected_section_keywords", [])]
        if not keywords:
            continue
        total += 1
        rows = search_sections(item["question"], limit=args.limit)
        rank = _first_relevant_rank(rows, keywords)
        hit = rank is not None
        hits += int(hit)
        reciprocal_rank_sum += (1.0 / rank) if rank else 0.0
        detail = f" rang={rank}" if rank else ""
        print(("OK " if hit else "KO ") + item["question"] + detail)
    recall = (hits / total * 100.0) if total else 0.0
    mrr = (reciprocal_rank_sum / total) if total else 0.0
    print(f"Recall attendu @ {args.limit}: {hits}/{total} ({recall:.1f}%)")
    print(f"MRR @ {args.limit}: {mrr:.3f}")


def cmd_codex_status(_args) -> None:
    binary = shutil.which(settings.codex_binary) or (settings.codex_binary if Path(settings.codex_binary).is_file() else None)
    if not binary:
        raise SystemExit("Codex CLI introuvable dans PATH.")
    version = subprocess.run([binary, "--version"], text=True, capture_output=True, check=False)
    print((version.stdout or version.stderr).strip())
    status = subprocess.run([binary, "login", "status"], text=True, capture_output=True, check=False)
    output = (status.stdout or status.stderr).strip()
    print(output or f"codex login status: code {status.returncode}")
    if status.returncode != 0:
        raise SystemExit(status.returncode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="etpos-assistant")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="Initialiser app.db et docs.db")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("create-user", help="Créer un utilisateur local")
    p.add_argument("--username")
    p.set_defaults(func=cmd_create_user)

    p = sub.add_parser("ingest", help="Télécharger et indexer les sources activées")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("search", help="Tester directement la recherche FTS5")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=6)
    p.add_argument("--debug", action="store_true", help="Afficher l'analyse, les variantes FTS et la fusion des scores")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("corpus-stats", help="Afficher l'état du corpus")
    p.set_defaults(func=cmd_corpus_stats)

    p = sub.add_parser("codex-status", help="Verifier Codex CLI et son authentification ChatGPT")
    p.set_defaults(func=cmd_codex_status)

    p = sub.add_parser("eval-retrieval", help="Mesurer le retrieval sur un fichier JSONL")
    p.add_argument("--path", default="eval/questions.example.jsonl")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_eval_retrieval)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
