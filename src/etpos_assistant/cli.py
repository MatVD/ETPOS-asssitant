from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import shutil
import subprocess
from pathlib import Path
from datetime import UTC, datetime

from .backups import (
    BackupError,
    create_app_backup,
    prune_app_backups,
    restore_app_database,
    validate_app_database,
)
from .config import settings
from .db import app_db, docs_db, init_all
from .docs_store import (
    DocsDatabaseError,
    activate_docs_database,
    list_docs_history,
    rollback_docs_database,
    validate_docs_database,
)
from .docs_update import build_docs_candidate
from .evaluation import (
    BenchmarkError,
    answer_result_to_dict,
    generate_answer_for_case,
    load_benchmark,
    retrieval_threshold_failures,
    rescore_answer_report,
    run_retrieval_benchmark,
    score_answer_case,
    summarize_answers,
)
from .ingestion import ingest_enabled_sources, load_registry
from .ingestion.service import registry_validation_benchmarks
from .ingestion.fetch import download_html, download_javascript
from .ingestion.parser import parse_html
from .ingestion.support import (
    ensure_same_origin_asset,
    extract_french_faq_items,
    faq_items_to_document,
    find_support_component_url,
    validate_faq_items,
)
from .ingestion.validation import SourceContentError, validate_source_html
from .providers.codex_cli import codex_auth_directory, codex_environment
from .retrieval import search_sections, search_sections_with_trace
from .security import hash_password


def cmd_init_db(_args) -> None:
    init_all()
    print("Bases initialisées.")


def cmd_backup_app_db(args) -> None:
    try:
        backup_path = create_app_backup(settings.app_db, settings.backup_dir)
        retention_days = (
            settings.backup_retention_days
            if args.retention_days is None
            else args.retention_days
        )
        deleted = prune_app_backups(settings.backup_dir, retention_days)
    except BackupError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Sauvegarde créée : {backup_path}")
    print(f"Rétention : {retention_days} jour(s), {len(deleted)} ancienne(s) sauvegarde(s) supprimée(s).")


def cmd_verify_app_backup(args) -> None:
    path = Path(args.path).expanduser()
    try:
        validate_app_database(path)
    except BackupError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Sauvegarde valide : {path}")


def cmd_restore_app_db(args) -> None:
    backup_path = Path(args.path).expanduser()
    target_path = Path(args.target).expanduser() if args.target else settings.app_db
    live_target = target_path.resolve() == settings.app_db.resolve()
    if live_target and not args.confirm_service_stopped:
        raise SystemExit(
            "Refus de restaurer app.db sans --confirm-service-stopped. "
            "Arrêter etpos-assistant.service avant une restauration en production."
        )
    try:
        restored = restore_app_database(backup_path, target_path)
    except BackupError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Base restaurée : {restored}")


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


async def _run_inspect_source(args) -> None:
    registry_path = Path(args.registry)
    try:
        sources = load_registry(registry_path, enabled_only=False)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Impossible de lire le registre {registry_path}: {exc}") from exc

    source = next((item for item in sources if str(item.get("id")) == args.source_id), None)
    if source is None:
        raise SystemExit(f"Source inconnue dans {registry_path}: {args.source_id}")

    try:
        html, digest = await download_html(source["url"])
    except Exception as exc:
        raise SystemExit(f"Téléchargement impossible pour {args.source_id}: {exc}") from exc

    validation_error: str | None = None
    validation_method = "html"
    component_url: str | None = None
    component_digest: str | None = None
    extracted_faqs: int | None = None
    try:
        validate_source_html(source, html)
        parsed = parse_html(html, source["url"])
    except SourceContentError as exc:
        validation_error = str(exc)
        parsed = parse_html(html, source["url"])
        if str(source.get("validation_profile") or "").strip().lower() == "support_faq_answers":
            try:
                component_url = find_support_component_url(html, source["url"])
                ensure_same_origin_asset(source["url"], component_url)
                bundle_text, component_digest = await download_javascript(component_url)
                faq_items = extract_french_faq_items(bundle_text)
                validate_faq_items(source, faq_items)
                parsed = faq_items_to_document(faq_items, source["url"])
                extracted_faqs = len(faq_items)
                validation_error = None
                validation_method = "static_client_bundle"
            except (SourceContentError, OSError, ValueError) as bundle_exc:
                validation_error = f"{validation_error} Fallback bundle: {bundle_exc}"

    report = {
        "id": source["id"],
        "name": source.get("name"),
        "url": source["url"],
        "type": source.get("type"),
        "enabled": bool(source.get("enabled", False)),
        "digest": digest,
        "validation_method": validation_method,
        "component_url": component_url,
        "component_digest": component_digest,
        "extracted_faqs": extracted_faqs,
        "document_title": parsed.title,
        "document_version": parsed.version,
        "revision_date": parsed.revision_date,
        "sections": len(parsed.sections),
        "total_source_chars": sum(len(section.source_text) for section in parsed.sections),
        "validation": "failed" if validation_error else "ok",
        "validation_error": validation_error,
        "section_preview": [
            {
                "title": section.title,
                "heading_path": section.heading_path,
                "chars": len(section.source_text),
            }
            for section in parsed.sections[:10]
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if validation_error:
        raise SystemExit("Source inspectée mais refusée par les garde-fous de qualité.")



def cmd_inspect_source(args) -> None:
    asyncio.run(_run_inspect_source(args))


def cmd_search(args) -> None:
    init_all()
    docs_path = Path(args.docs_db).expanduser() if args.docs_db else None
    if args.debug:
        rows, trace = search_sections_with_trace(args.query, limit=args.limit, db_path=docs_path)
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
        rows = search_sections(args.query, limit=args.limit, db_path=docs_path)

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



def cmd_eval_retrieval(args) -> None:
    benchmark_path = Path(args.path)
    docs_path = Path(args.docs_db).expanduser() if args.docs_db else settings.docs_db
    try:
        validate_docs_database(docs_path, require_delete_journal=False)
        cases = load_benchmark(benchmark_path)
    except (OSError, BenchmarkError, DocsDatabaseError) as exc:
        raise SystemExit(str(exc)) from exc

    results, summary = run_retrieval_benchmark(cases, args.limit, db_path=docs_path)
    for result in results:
        case = result.case
        if case.answerability == "none":
            print(f"INFO [{case.category}] {case.case_id} non-répondable (non scoré au retrieval)")
            continue
        rank = result.first_relevant_rank
        detail = f"rang={rank}" if rank else "aucun passage attendu"
        coverage = f"groupes={result.matched_groups}/{result.expected_groups}"
        print(
            f"{'OK' if result.hit else 'KO'} [{case.category}] {case.case_id} "
            f"{detail} {coverage} {result.latency_ms:.1f}ms — {case.question}"
        )
    print(
        f"Recall@{args.limit}: {summary['recall_at_k'] * 100:.1f}% "
        f"({sum(result.hit for result in results if result.case.answerability != 'none')}/"
        f"{summary['answerable_cases']})"
    )
    print(f"MRR@{args.limit}: {summary['mrr']:.3f}")
    print(f"Couverture des groupes de passages@{args.limit}: {summary['group_coverage'] * 100:.1f}%")
    print(
        f"Latence retrieval: moyenne {summary['latency_mean_ms']:.1f}ms, "
        f"p95 {summary['latency_p95_ms']:.1f}ms"
    )
    print(f"Cas non répondables réservés à l'évaluation réponse: {summary['unanswerable_cases']}")
    for category, metrics in summary["categories"].items():
        print(
            f"  - {category}: n={metrics['cases']} "
            f"Recall@{args.limit}={metrics['recall_at_k'] * 100:.1f}% "
            f"MRR={metrics['mrr']:.3f} "
            f"couverture={metrics['group_coverage'] * 100:.1f}%"
        )

    try:
        failures = retrieval_threshold_failures(
            summary,
            min_recall=args.min_recall,
            min_mrr=args.min_mrr,
            min_group_coverage=args.min_group_coverage,
        )
    except BenchmarkError as exc:
        raise SystemExit(str(exc)) from exc
    if failures:
        raise SystemExit("Échec des seuils retrieval : " + "; ".join(failures))


async def _run_update_docs(args) -> None:
    extra_source_ids = tuple(args.candidate_source or ())
    if extra_source_ids and not args.dry_run:
        raise SystemExit("--candidate-source est réservé au mode --dry-run et ne peut pas activer une source désactivée.")
    registry_path = Path(args.registry)
    try:
        automatic_validation_paths = registry_validation_benchmarks(
            registry_path,
            extra_source_ids=extra_source_ids,
        )
        candidate = await build_docs_candidate(
            registry_path=registry_path,
            extra_source_ids=extra_source_ids,
        )
    except (OSError, ValueError, DocsDatabaseError) as exc:
        raise SystemExit(str(exc)) from exc

    print(json.dumps(candidate.as_dict(), ensure_ascii=False, indent=2))
    if candidate.status == "unchanged":
        print("Corpus inchangé : aucune activation nécessaire.")
        return
    if candidate.candidate_path is None:
        raise SystemExit("La reconstruction n'a produit aucune base candidate exploitable.")

    try:
        cases = load_benchmark(Path(args.benchmark))
        _results, summary = run_retrieval_benchmark(
            cases,
            args.limit,
            db_path=candidate.candidate_path,
        )
        failures = retrieval_threshold_failures(
            summary,
            min_recall=args.min_recall,
            min_mrr=args.min_mrr,
            min_group_coverage=args.min_group_coverage,
        )
    except (OSError, BenchmarkError, DocsDatabaseError) as exc:
        raise SystemExit(str(exc)) from exc

    print(
        f"Candidate retrieval: Recall@{args.limit}={summary['recall_at_k']:.3f}, "
        f"MRR={summary['mrr']:.3f}, couverture={summary['group_coverage']:.3f}, "
        f"p95={summary['latency_p95_ms']:.1f}ms"
    )
    if failures:
        raise SystemExit(
            "Candidate refusée par les seuils retrieval : "
            + "; ".join(failures)
            + f". Base conservée pour inspection : {candidate.candidate_path}"
        )

    validation_paths: list[str] = []
    for validation_path_value in (*automatic_validation_paths, *(args.validation_benchmark or ())):
        if validation_path_value not in validation_paths:
            validation_paths.append(validation_path_value)

    for validation_path_value in validation_paths:
        validation_path = Path(validation_path_value)
        try:
            validation_cases = load_benchmark(validation_path)
            _validation_results, validation_summary = run_retrieval_benchmark(
                validation_cases,
                args.limit,
                db_path=candidate.candidate_path,
            )
            validation_failures = retrieval_threshold_failures(
                validation_summary,
                min_recall=args.min_recall,
                min_group_coverage=args.min_group_coverage,
            )
        except (OSError, BenchmarkError, DocsDatabaseError) as exc:
            raise SystemExit(str(exc)) from exc
        print(
            f"Candidate validation {validation_path}: "
            f"Recall@{args.limit}={validation_summary['recall_at_k']:.3f}, "
            f"MRR={validation_summary['mrr']:.3f}, "
            f"couverture={validation_summary['group_coverage']:.3f}, "
            f"p95={validation_summary['latency_p95_ms']:.1f}ms"
        )
        if validation_failures:
            raise SystemExit(
                f"Candidate refusée par {validation_path}: "
                + "; ".join(validation_failures)
                + f". Base conservée pour inspection : {candidate.candidate_path}"
            )

    if args.dry_run:
        print(f"Dry-run validé. Candidate non activée : {candidate.candidate_path}")
        return

    try:
        activation = activate_docs_database(
            candidate.candidate_path,
            keep_history=args.keep_history,
        )
    except DocsDatabaseError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({"activation": activation}, ensure_ascii=False, indent=2))


def cmd_update_docs_db(args) -> None:
    asyncio.run(_run_update_docs(args))


def cmd_docs_history(_args) -> None:
    history = list_docs_history()
    if not history:
        print("Aucune ancienne docs.db conservée.")
        return
    for path in history:
        try:
            stats = validate_docs_database(path, require_delete_journal=True)
            print(
                f"{path} — documents={stats['documents']} sections={stats['sections']} "
                f"journal={stats['journal_mode']}"
            )
        except DocsDatabaseError as exc:
            print(f"INVALIDE {path} — {exc}")


def cmd_rollback_docs_db(args) -> None:
    try:
        result = rollback_docs_database(
            Path(args.path).expanduser(),
            keep_history=args.keep_history,
        )
    except DocsDatabaseError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({"rollback": result}, ensure_ascii=False, indent=2))


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _print_answer_summary(summary: dict, *, include_latency: bool = True) -> None:
    print(f"Abstention correcte: {_pct(summary['abstention_accuracy'])}")
    print(
        f"Faits obligatoires: {_pct(summary['fact_coverage'])} "
        f"({summary['fact_matches']}/{summary['fact_total']})"
    )
    print(
        f"Chemins de menus: {_pct(summary['menu_path_coverage'])} "
        f"({summary['menu_matches']}/{summary['menu_total']})"
    )
    print(f"Présence des citations: {_pct(summary['citation_presence_accuracy'])}")
    print(
        f"Couverture des passages attendus par les citations: "
        f"{_pct(summary['citation_expected_coverage'])} "
        f"({summary['matched_citation_groups']}/{summary['expected_citation_groups']})"
    )
    print(
        f"Part des citations dans les passages attendus: {_pct(summary['citation_relevance'])} "
        f"({summary['relevant_citations']}/{summary['citations']})"
    )
    if include_latency:
        print(
            f"Latence réponse complète: moyenne {summary['latency_mean_ms']:.0f}ms, "
            f"p95 {summary['latency_p95_ms']:.0f}ms"
        )


async def _run_eval_answer(args) -> None:
    path = Path(args.path)
    try:
        cases = load_benchmark(path)
    except (OSError, BenchmarkError) as exc:
        raise SystemExit(str(exc)) from exc

    if args.case_id:
        selected_ids = set(args.case_id)
        known_ids = {case.case_id for case in cases}
        unknown_ids = sorted(selected_ids - known_ids)
        if unknown_ids:
            raise SystemExit(f"Cas inconnus: {', '.join(unknown_ids)}")
        cases = [case for case in cases if case.case_id in selected_ids]
    if args.category:
        selected_categories = set(args.category)
        cases = [case for case in cases if case.category in selected_categories]
    if args.max_cases is not None:
        if args.max_cases < 1:
            raise SystemExit("--max-cases doit être supérieur ou égal à 1.")
        cases = cases[: args.max_cases]
    if not cases:
        raise SystemExit("Aucun cas du benchmark ne correspond aux filtres demandés.")

    docs_path = Path(args.docs_db).expanduser() if args.docs_db else None
    if docs_path is not None:
        try:
            validate_docs_database(docs_path, require_delete_journal=False)
        except DocsDatabaseError as exc:
            raise SystemExit(str(exc)) from exc

    results = []
    for case in cases:
        answer = await generate_answer_for_case(case, limit=args.limit, db_path=docs_path)
        result = score_answer_case(case, answer)
        results.append(result)
        facts = f"faits={result.matched_facts}/{result.expected_facts}" if result.expected_facts else "faits=n/a"
        menus = (
            f"menus={result.matched_menu_paths}/{result.expected_menu_paths}"
            if result.expected_menu_paths
            else "menus=n/a"
        )
        citations = f"citations={result.relevant_citations}/{result.total_citations}"
        print(
            f"{'OK' if result.abstention_correct else 'KO'} [{case.category}] {case.case_id} "
            f"abstention={'ok' if result.abstention_correct else 'ko'} {facts} {menus} {citations} "
            f"{answer.total_latency_ms:.0f}ms — {case.question}"
        )

    summary = summarize_answers(results)
    if args.json_output:
        output_path = Path(args.json_output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        git_sha = None
        git_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
        if git_result.returncode == 0:
            git_sha = git_result.stdout.strip() or None
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "benchmark_path": str(path),
            "provider": settings.provider,
            "model": settings.codex_model or None,
            "git_sha": git_sha,
            "docs_db": str(docs_path) if docs_path is not None else str(settings.docs_db),
            "limit": args.limit,
            "summary": summary,
            "cases": [answer_result_to_dict(result) for result in results],
        }
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Rapport JSON: {output_path}")
    _print_answer_summary(summary)
    print("Hallucinations: non mesurées automatiquement par cette commande.")


def cmd_eval_answer(args) -> None:
    if settings.provider != "codex":
        raise SystemExit(
            "eval-answer exige ETPOS_PROVIDER=codex afin de mesurer la réponse du provider de production."
        )
    init_all()
    asyncio.run(_run_eval_answer(args))


def cmd_rescore_answer_report(args) -> None:
    benchmark_path = Path(args.benchmark).expanduser()
    report_path = Path(args.report).expanduser()
    docs_db_path = Path(args.docs_db).expanduser()
    try:
        results, summary = rescore_answer_report(benchmark_path, report_path, docs_db_path)
    except BenchmarkError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"Re-scoring sans appel Codex : {report_path}")
    _print_answer_summary(summary)
    review = [
        result
        for result in results
        if not result.abstention_correct
        or (result.expected_facts and result.matched_facts < result.expected_facts)
        or (result.expected_menu_paths and result.matched_menu_paths < result.expected_menu_paths)
        or not result.citation_presence_correct
    ]
    if review:
        print("Cas à revoir :")
        for result in review:
            print(
                f"  - {result.case.case_id}: "
                f"answerability={result.case.answerability} "
                f"abstention={'ok' if result.abstention_correct else 'ko'} "
                f"faits={result.matched_facts}/{result.expected_facts} "
                f"menus={result.matched_menu_paths}/{result.expected_menu_paths} "
                f"citations={'ok' if result.citation_presence_correct else 'ko'} "
                f"passages={result.relevant_citations}/{result.total_citations}"
            )


def cmd_codex_status(_args) -> None:
    binary = shutil.which(settings.codex_binary) or (settings.codex_binary if Path(settings.codex_binary).is_file() else None)
    if not binary:
        raise SystemExit("Codex CLI introuvable dans PATH.")
    env = codex_environment()
    auth_dir = codex_auth_directory(env)
    print(f"Codex binaire : {binary}")
    print(f"Répertoire d'authentification effectif : {auth_dir or 'indéterminé'}")
    print(f"CODEX_HOME explicite : {'oui' if env.get('CODEX_HOME') else 'non'}")
    version = subprocess.run(
        [binary, "--version"],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    print((version.stdout or version.stderr).strip())
    status = subprocess.run(
        [binary, "login", "status"],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    output = (status.stdout or status.stderr).strip()
    print(output or f"codex login status: code {status.returncode}")
    if status.returncode != 0:
        raise SystemExit(status.returncode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="etpos-assistant")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="Initialiser app.db et docs.db")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("backup-app-db", help="Sauvegarder app.db avec l'API SQLite backup")
    p.add_argument("--retention-days", type=int)
    p.set_defaults(func=cmd_backup_app_db)

    p = sub.add_parser("verify-app-backup", help="Vérifier l'intégrité et le schéma d'une sauvegarde app.db")
    p.add_argument("path")
    p.set_defaults(func=cmd_verify_app_backup)

    p = sub.add_parser("restore-app-db", help="Restaurer une sauvegarde app.db vers une base cible")
    p.add_argument("path", help="Fichier de sauvegarde à restaurer")
    p.add_argument("--target", help="Cible explicite pour tester une restauration sans remplacer app.db")
    p.add_argument(
        "--confirm-service-stopped",
        action="store_true",
        help="Requis pour remplacer app.db ; confirme que le service applicatif est arrêté",
    )
    p.set_defaults(func=cmd_restore_app_db)

    p = sub.add_parser("create-user", help="Créer un utilisateur local")
    p.add_argument("--username")
    p.set_defaults(func=cmd_create_user)

    p = sub.add_parser("ingest", help="Télécharger et indexer les sources activées")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser(
        "inspect-source",
        help="Télécharger et analyser une source déclarée sans modifier docs.db",
    )
    p.add_argument("source_id", help="Identifiant de source présent dans config/sources.json")
    p.add_argument("--registry", default="config/sources.json")
    p.set_defaults(func=cmd_inspect_source)

    p = sub.add_parser("search", help="Tester directement la recherche FTS5")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=6)
    p.add_argument("--docs-db", help="Base documentaire explicite à interroger")
    p.add_argument("--debug", action="store_true", help="Afficher l'analyse, les variantes FTS et la fusion des scores")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("corpus-stats", help="Afficher l'état du corpus")
    p.set_defaults(func=cmd_corpus_stats)

    p = sub.add_parser("codex-status", help="Verifier Codex CLI et son authentification ChatGPT")
    p.set_defaults(func=cmd_codex_status)

    p = sub.add_parser("eval-retrieval", help="Mesurer le retrieval sur un benchmark JSONL")
    p.add_argument("--path", default="eval/benchmark.jsonl")
    p.add_argument("--docs-db", help="Base documentaire explicite à évaluer sans l'activer")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--min-recall", type=float, help="Seuil Recall@K minimal entre 0 et 1")
    p.add_argument("--min-mrr", type=float, help="Seuil MRR@K minimal entre 0 et 1")
    p.add_argument(
        "--min-group-coverage",
        type=float,
        help="Seuil minimal de couverture des groupes attendus entre 0 et 1",
    )
    p.set_defaults(func=cmd_eval_retrieval)

    p = sub.add_parser(
        "update-docs-db",
        help="Détecter, reconstruire, tester puis activer atomiquement docs.db",
    )
    p.add_argument("--registry", default="config/sources.json")
    p.add_argument("--benchmark", default="eval/benchmark.jsonl")
    p.add_argument(
        "--validation-benchmark",
        action="append",
        help="Benchmark supplémentaire appliqué à la candidate ; Recall/couverture sont des garde-fous, MRR est reporté",
    )
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--min-recall", type=float, default=0.95)
    p.add_argument("--min-mrr", type=float, default=0.80)
    p.add_argument("--min-group-coverage", type=float, default=0.95)
    p.add_argument("--keep-history", type=int, default=settings.docs_history_keep)
    p.add_argument(
        "--candidate-source",
        action="append",
        help="Inclure une source désactivée uniquement dans une candidate dry-run ; option répétable",
    )
    p.add_argument("--dry-run", action="store_true", help="Construire et tester sans activer")
    p.set_defaults(func=cmd_update_docs_db)

    p = sub.add_parser("docs-history", help="Lister les anciennes bases documentaires conservées")
    p.set_defaults(func=cmd_docs_history)

    p = sub.add_parser("rollback-docs-db", help="Réactiver atomiquement une docs.db archivée")
    p.add_argument("path", help="Chemin d'une base présente dans data/docs-history")
    p.add_argument("--keep-history", type=int, default=settings.docs_history_keep)
    p.set_defaults(func=cmd_rollback_docs_db)

    p = sub.add_parser("eval-answer", help="Mesurer les réponses finales avec le provider Codex")
    p.add_argument("--path", default="eval/benchmark.jsonl")
    p.add_argument("--docs-db", help="Base documentaire explicite à utiliser sans l'activer")
    p.add_argument("--limit", type=int, default=6, help="Nombre de passages injectés dans la réponse")
    p.add_argument(
        "--case-id",
        action="append",
        help="Limiter à un identifiant de cas précis ; option répétable",
    )
    p.add_argument(
        "--category",
        action="append",
        help="Limiter à une catégorie du benchmark ; option répétable",
    )
    p.add_argument("--max-cases", type=int, help="Limiter le nombre de cas exécutés")
    p.add_argument(
        "--json-output",
        help="Écrire un rapport JSON détaillé pour comparaison et revue humaine",
    )
    p.set_defaults(func=cmd_eval_answer)

    p = sub.add_parser(
        "rescore-answer-report",
        help="Recalculer un rapport eval-answer existant sans rappeler Codex",
    )
    p.add_argument("--benchmark", default="eval/benchmark.jsonl")
    p.add_argument("--report", required=True)
    p.add_argument("--docs-db", default=str(settings.docs_db))
    p.set_defaults(func=cmd_rescore_answer_report)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
