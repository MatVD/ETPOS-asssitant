from pathlib import Path
from types import SimpleNamespace

import etpos_assistant.db as db_module
from etpos_assistant.db import docs_db, init_docs_db
from etpos_assistant.retrieval import (
    RetrievalTuning,
    build_fts_query,
    build_search_plan,
    search_sections,
    search_sections_with_trace,
)
from etpos_assistant.vocabulary import analyze_query


def _settings(tmp_path: Path):
    data_dir = tmp_path / "data"
    return SimpleNamespace(
        data_dir=data_dir,
        app_db=data_dir / "app.db",
        docs_db=data_dir / "docs.db",
        ensure_dirs=lambda: data_dir.mkdir(parents=True, exist_ok=True),
    )


def _create_document(monkeypatch, tmp_path: Path) -> int:
    monkeypatch.setattr(db_module, "settings", _settings(tmp_path))
    init_docs_db()
    with docs_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO documents(source_key,name,source_url,source_type,source_priority,language,retrieved_at,content_hash,snapshot_path)
            VALUES ('manual','Manuel','https://example.test','manual',100,'fr','2026-01-01','abc','snapshot')
            """
        )
        return int(cursor.lastrowid)


def _insert_section(
    document_id: int,
    *,
    order: int,
    title: str,
    path: str,
    text: str,
) -> None:
    with docs_db() as conn:
        conn.execute(
            """
            INSERT INTO sections(document_id,section_order,title,heading_path,source_url,source_text,search_text)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                document_id,
                order,
                title,
                path,
                f"https://example.test#{order}",
                text,
                f"{title} {path} {text}",
            ),
        )


def test_fts5_ranks_exact_etpos_terms(monkeypatch, tmp_path):
    document_id = _create_document(monkeypatch, tmp_path)
    _insert_section(
        document_id,
        order=1,
        title="Sauvegarde",
        path="Système > Sauvegarde",
        text="Procédure sauvegarde manuelle et automatique",
    )
    _insert_section(
        document_id,
        order=2,
        title="Utilisateurs",
        path="Fichier > Utilisateurs",
        text="Gestion des profils utilisateurs",
    )

    rows = search_sections("Comment faire une sauvegarde manuelle ?", limit=2)
    assert rows
    assert rows[0].title == "Sauvegarde"
    assert "sauvegarde" in build_fts_query("Sauvegarde manuelle")


def test_sale_account_creation_ranks_main_concept_before_secondary_meanings(monkeypatch, tmp_path):
    document_id = _create_document(monkeypatch, tmp_path)
    _insert_section(
        document_id,
        order=1,
        title="ETPOS POUR COMMERCE DE DÉTAIL",
        path="ETPOS POUR COMMERCE DE DÉTAIL",
        text=(
            "Dans le commerce de détail le type d’enregistrement généralement utilisé est Comptes. "
            "Pour enregistrer Comptes, sélectionner Compte, sélectionner famille, sélectionner les articles "
            "et enregistrer le compte."
        ),
    )
    _insert_section(
        document_id,
        order=2,
        title="Diviser le compte",
        path="ETPOS POUR RESTAURATION OU SIMILAIRE > Configurer salles et gérer tables > Diviser le compte",
        text="Divise automatiquement le compte d’une table pour le nombre indiqué de personnes.",
    )
    _insert_section(
        document_id,
        order=3,
        title="Créer reçu",
        path="GESTION DES CLIENTS > Gérer comptes courants des clients > Créer reçu",
        text="Crée un nouveau reçu pour un client et met à jour le compte courant du client.",
    )
    _insert_section(
        document_id,
        order=4,
        title="Types de règlement",
        path="CONFIGURER ETPOS > Types de règlement",
        text="Pour ajouter un nouveau type de règlement, configurer aussi le mouvement du compte courant.",
    )

    plan = build_search_plan("Comment ajouter un nouveau compte ?")
    assert any(item.label.startswith("COMPTE_VENTE:") for item in plan)

    rows = search_sections("Comment ajouter un nouveau compte ?", limit=4)
    paths = [row.heading_path for row in rows]
    retail_index = paths.index("ETPOS POUR COMMERCE DE DÉTAIL")
    divide_index = next(index for index, path in enumerate(paths) if "Diviser le compte" in path)
    current_index = next(index for index, path in enumerate(paths) if "comptes courants" in path)

    assert retail_index < divide_index
    assert retail_index < current_index


def test_account_current_client_keeps_its_specific_meaning(monkeypatch, tmp_path):
    document_id = _create_document(monkeypatch, tmp_path)
    _insert_section(
        document_id,
        order=1,
        title="ETPOS POUR COMMERCE DE DÉTAIL",
        path="ETPOS POUR COMMERCE DE DÉTAIL",
        text="Pour enregistrer Comptes, sélectionner Compte puis enregistrer le compte.",
    )
    _insert_section(
        document_id,
        order=2,
        title="Comptes courant",
        path="GESTION DES CLIENTS > Gérer les comptes courant des clients > Comptes courant",
        text="La gestion des comptes courants des clients est effectuée dans Options puis Reçus.",
    )

    rows = search_sections("Comment fonctionne un compte courant client ?", limit=2)
    assert rows[0].title == "Comptes courant"


def test_divide_account_is_not_reinterpreted_as_account_creation(monkeypatch, tmp_path):
    document_id = _create_document(monkeypatch, tmp_path)
    _insert_section(
        document_id,
        order=1,
        title="ETPOS POUR COMMERCE DE DÉTAIL",
        path="ETPOS POUR COMMERCE DE DÉTAIL",
        text="Pour enregistrer Comptes, sélectionner Compte puis enregistrer le compte.",
    )
    _insert_section(
        document_id,
        order=2,
        title="Diviser le compte",
        path="ETPOS POUR RESTAURATION OU SIMILAIRE > Diviser le compte",
        text="Pour diviser le compte, choisir Diviser le compte puis sélectionner la table.",
    )

    plan = build_search_plan("Comment diviser un compte ?")
    assert [item.label for item in plan] == ["lexical"]
    rows = search_sections("Comment diviser un compte ?", limit=2)
    assert rows[0].title == "Diviser le compte"


def test_user_concept_beats_unrelated_create_actions(monkeypatch, tmp_path):
    document_id = _create_document(monkeypatch, tmp_path)
    _insert_section(
        document_id,
        order=1,
        title="Que sont les utilisateurs",
        path="GESTION DES UTILISATEURS > Que sont les utilisateurs",
        text="Les utilisateurs sont les personnes qui enregistrent et configurent le logiciel. Accès au Fichier utilisateurs.",
    )
    _insert_section(
        document_id,
        order=2,
        title="Créer liquidation",
        path="GESTION DES FOURNISSEURS > Créer liquidation",
        text="Créer une liquidation. Le numéro d’utilisateur actif est mémorisé dans le document.",
    )

    rows = search_sections("Comment créer un utilisateur ?", limit=2)
    assert rows[0].heading_path.startswith("GESTION DES UTILISATEURS")


def test_query_analysis_separates_intent_object_and_concept():
    analysis = analyze_query("Comment ajouter un nouveau compte ?")
    assert analysis.intents == ("CREATE",)
    assert analysis.objects == ("COMPTE",)
    assert analysis.qualifiers == ()
    assert [concept.key for concept in analysis.concepts] == ["COMPTE_VENTE"]


def test_sale_account_expansion_uses_domain_vocabulary_not_document_location():
    plan = build_search_plan("Comment ouvrir un compte ?")
    concept_queries = [item.query for item in plan if item.label.startswith("COMPTE_VENTE:")]
    assert concept_queries
    assert all("commerce" not in query and "detail" not in query for query in concept_queries)
    assert any("famille" in query and "article" in query for query in concept_queries)


def test_retrieval_trace_exposes_variants_and_score_components(monkeypatch, tmp_path):
    document_id = _create_document(monkeypatch, tmp_path)
    _insert_section(
        document_id,
        order=1,
        title="Vente",
        path="Utilisation > Vente",
        text="Sélectionner Compte, sélectionner famille, sélectionner les articles puis enregistrer le compte.",
    )
    _insert_section(
        document_id,
        order=2,
        title="Diviser le compte",
        path="Restauration > Diviser le compte",
        text="Diviser le compte d’une table.",
    )

    rows, trace = search_sections_with_trace("Comment ajouter un compte ?", limit=2)
    assert rows[0].title == "Vente"
    assert trace.analysis.intents == ("CREATE",)
    assert trace.analysis.objects == ("COMPTE",)
    assert len(trace.variants) > 1
    assert trace.candidates
    assert trace.candidates[0].matched_variants
    assert trace.candidates[0].final_score == trace.candidates[0].rrf_score + trace.candidates[0].signal_score


def test_current_account_meanings_are_disambiguated():
    client = analyze_query("Comment fonctionne un compte courant client ?")
    supplier = analyze_query("Comment fonctionne un compte courant fournisseur ?")
    generic = analyze_query("Comment fonctionne un compte courant ?")

    assert [concept.key for concept in client.concepts] == ["COMPTE_COURANT_CLIENT"]
    assert [concept.key for concept in supplier.concepts] == ["COMPTE_COURANT_FOURNISSEUR"]
    assert [concept.key for concept in generic.concepts] == ["COMPTE_COURANT"]


def test_retrieval_tuning_is_explicit_and_overrideable():
    tuning = RetrievalTuning(title_weight=3.0, heading_path_weight=2.0, body_weight=1.0)
    assert tuning.title_weight == 3.0
    assert tuning.heading_path_weight == 2.0
    assert tuning.body_weight == 1.0
