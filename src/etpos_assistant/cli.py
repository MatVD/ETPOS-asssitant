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
from .retrieval import search_sections
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
    rows = search_sections(args.query, limit=args.limit)
    if not rows:
        print("Aucun résultat.")
        return
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



def cmd_eval_retrieval(args) -> None:
    init_all()
    path = args.path
    total = 0
    hits = 0
    for raw in open(path, encoding="utf-8"):
        raw = raw.strip()
        if not raw:
            continue
        item = json.loads(raw)
        if not item.get("answerable", True):
            continue
        total += 1
        rows = search_sections(item["question"], limit=args.limit)
        keywords = [str(k).lower() for k in item.get("expected_section_keywords", [])]
        haystacks = [f"{r.title} {r.heading_path} {r.source_text}".lower() for r in rows]
        hit = bool(haystacks) and all(any(keyword in h for h in haystacks) for keyword in keywords)
        hits += int(hit)
        print(("OK " if hit else "KO ") + item["question"])
    score = (hits / total * 100.0) if total else 0.0
    print(f"Recall attendu @ {args.limit}: {hits}/{total} ({score:.1f}%)")


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
