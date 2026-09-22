from pathlib import Path
from types import SimpleNamespace

import etpos_assistant.db as db_module
from etpos_assistant.db import docs_db, init_docs_db
from etpos_assistant.retrieval import (
    CandidateTrace,
    RetrievalTuning,
    RetrievedSection,
    _select_diverse_candidates,
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
    _insert_section(
        document_id,
        order=5,
        title="Que sont les utilisateurs",
        path="GESTION DES UTILISATEURS > Que sont les utilisateurs",
        text="Les utilisateurs sont les personnes qui enregistrent et configurent le logiciel.",
    )

    plan = build_search_plan("Comment ajouter un nouveau compte ?")
    labels = {item.label.split(":", 1)[0] for item in plan}
    assert {"COMPTE_VENTE", "COMPTE_COURANT_CLIENT", "UTILISATEUR"} <= labels

    rows = search_sections("Comment ajouter un nouveau compte ?", limit=5)
    paths = [row.heading_path for row in rows]

    assert "ETPOS POUR COMMERCE DE DÉTAIL" in paths
    assert any("comptes courants" in path for path in paths)
    assert any(path.startswith("GESTION DES UTILISATEURS") for path in paths)


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
    assert any(item.label.startswith("DIVISION_COMPTE:") for item in plan)
    assert not any(item.label.startswith("COMPTE_VENTE:") for item in plan)
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


def test_client_file_concept_targets_client_record_not_current_account(monkeypatch, tmp_path):
    document_id = _create_document(monkeypatch, tmp_path)
    _insert_section(
        document_id,
        order=1,
        title="Gestion des Clients",
        path="GESTION DES CLIENTS > Gestion des Clients",
        text=(
            "Pour gérer des clients, aller dans Fichiers + Fichier de clients. "
            "Le fichier des clients stocke les informations sur les clients et les données de facturation."
        ),
    )
    _insert_section(
        document_id,
        order=2,
        title="Configurer les documents",
        path="GESTION DES CLIENTS > Gérer les comptes courant des clients > Comptes courant > Configurer les documents",
        text="Configurer les documents du compte courant client.",
    )

    analysis = analyze_query("Où modifier la fiche d'un client ?")
    assert [concept.key for concept in analysis.concepts] == ["CLIENT_FICHIER"]

    rows = search_sections("Où modifier la fiche d'un client ?", limit=2)
    assert rows[0].title == "Gestion des Clients"

    current_account = analyze_query("Comment gérer un compte courant client ?")
    assert "CLIENT_FICHIER" not in [concept.key for concept in current_account.concepts]


def test_query_analysis_separates_intent_object_and_concept():
    analysis = analyze_query("Comment ajouter un nouveau compte ?")
    assert analysis.intents == ("CREATE",)
    assert analysis.objects == ("COMPTE",)
    assert analysis.qualifiers == ()
    assert [concept.key for concept in analysis.concepts] == [
        "COMPTE_COURANT_CLIENT",
        "UTILISATEUR",
        "COMPTE_VENTE",
    ]


def test_sale_account_expansion_uses_domain_vocabulary_not_document_location():
    plan = build_search_plan("Comment ouvrir un compte ?")
    concept_queries = [item.query for item in plan if item.label.startswith("COMPTE_VENTE:")]
    assert concept_queries
    assert all("commerce" not in query and "detail" not in query for query in concept_queries)
    assert any("famille" in query and "article" in query for query in concept_queries)


def test_account_with_article_qualifier_is_not_expanded_to_unrelated_meanings():
    analysis = analyze_query(
        "Je veux commencer un nouveau compte pour enregistrer ses articles."
    )

    assert [concept.key for concept in analysis.concepts] == ["ARTICLE", "COMPTE_VENTE"]


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


def test_natural_domain_phrasing_expands_to_documented_concepts():
    backup = build_search_plan("Je veux faire une copie de sécurité de ma caisse")
    permissions = build_search_plan("Je dois limiter ce qu'un employé a le droit de faire")
    split_payment = build_search_plan("Le client veut payer une partie en espèces et le reste par carte")
    implicit_user = build_search_plan("Je crée la personne qui va se connecter et utiliser la caisse")

    assert any(item.label.startswith("SAUVEGARDE:") for item in backup)
    assert any(item.label.startswith("PERMISSIONS_UTILISATEUR:") for item in permissions)
    assert any(item.label.startswith("REGLEMENT_MIXTE:") for item in split_payment)
    assert any(item.label.startswith("UTILISATEUR:") for item in implicit_user)


def test_controlled_typo_correction_recovers_domain_concepts():
    backup = build_search_plan("Coment faire une sauvgarde manuele ?")
    permissions = build_search_plan("Ou regler les permisions d'un utilisteur ?")
    supplier = build_search_plan("Voir le compte courrant d'un fourniisseur")

    assert any(item.label == "corrected" for item in backup)
    assert any(item.label.startswith("SAUVEGARDE:") for item in backup)
    assert any(item.label.startswith("PERMISSIONS_UTILISATEUR:") for item in permissions)
    assert any(item.label.startswith("COMPTE_COURANT_FOURNISSEUR:") for item in supplier)


def test_realistic_multi_intent_and_ambiguous_phrasing_expand_to_concepts():
    payments = analyze_query(
        "Créer un type de paiement puis combiner deux moyens de règlement sur une vente"
    )
    backup = analyze_query("Faire une sauvegarde manuelle et la restaurer ensuite")
    restaurant = analyze_query(
        "Organiser mes salles et mes tables puis partager l'addition d'une table"
    )
    customer_account = analyze_query("Comment consulter ce qu'un client a sur son compte ?")

    assert {"TYPE_REGLEMENT", "REGLEMENT_MIXTE"} <= {c.key for c in payments.concepts}
    assert {"SAUVEGARDE_MANUELLE", "RESTAURATION_SAUVEGARDE"} <= {
        c.key for c in backup.concepts
    }
    assert {"SALLES_TABLES", "DIVISION_COMPTE"} <= {c.key for c in restaurant.concepts}
    assert {"COMPTE_COURANT_CLIENT", "COMPTE_VENTE"} <= {
        c.key for c in customer_account.concepts
    }


def test_card_points_and_person_ambiguity_keep_documented_meanings():
    card = analyze_query("Où je gère la carte et les points du client ?")
    person = analyze_query("Je dois ajouter une nouvelle personne dans ETPOS")

    assert "CARTE_POINTS" in {c.key for c in card.concepts}
    assert {"CLIENT_FICHIER", "UTILISATEUR"} <= {c.key for c in person.concepts}


def test_automatic_backup_verb_expands_to_documented_concept():
    plan = build_search_plan("Comment sauvegarder automatiquement ETPOS dans le cloud ?")
    assert any(item.label.startswith("SAUVEGARDE_AUTOMATIQUE:") for item in plan)


def test_benchmark_synonyms_expand_to_documented_etpos_terms():
    article_plan = build_search_plan("Où gère-t-on les articles ?")
    payment_plan = build_search_plan("Comment encaisser avec deux moyens de paiement différents ?")
    weighing_plan = build_search_plan("Comment passer la caisse en mode pesée ?")

    assert any(item.label.startswith("ARTICLE:") for item in article_plan)
    assert any(item.label.startswith("REGLEMENT_MIXTE:") for item in payment_plan)
    assert any(item.label.startswith("MODE_BALANCE:") for item in weighing_plan)


def test_ambiguous_client_account_keeps_both_plausible_business_meanings():
    analysis = analyze_query("Je veux ouvrir un compte pour un client, comment faire ?")
    keys = [concept.key for concept in analysis.concepts]
    assert "COMPTE_COURANT_CLIENT" in keys
    assert "COMPTE_VENTE" in keys


def test_generic_new_card_expands_multiple_etpos_card_meanings():
    plan = build_search_plan("Comment ajouter une nouvelle carte ?")
    card_queries = [item.query for item in plan if item.label.startswith("CARTE_GENERIQUE:")]
    assert any("rfid" in query for query in card_queries)
    assert any("consommation" in query for query in card_queries)


def test_lexical_anchor_is_kept_when_concept_reranking_would_crowd_it_out():
    concepts = analyze_query("Comment créer un utilisateur ?").concepts
    ranked = []
    for index in range(1, 7):
        section = RetrievedSection(
            id=index,
            title=f"Section {index}",
            heading_path=f"Chemin {index}",
            source_url=f"https://example.test/{index}",
            source_text=f"Texte {index}",
            document_name="ETPOS",
            document_version="5.34",
            revision_date="2026-01-30",
            document_hash="hash",
            score=-float(7 - index),
        )
        trace = CandidateTrace(
            section_id=index,
            title=section.title,
            heading_path=section.heading_path,
            rrf_score=0.1,
            signal_score=0.0,
            best_bm25_score=-float(7 - index),
            final_score=0.1,
            matched_variants=("UTILISATEUR:1",),
        )
        ranked.append((section, trace))

    selected = _select_diverse_candidates(
        ranked,
        concepts,
        limit=5,
        tuning=RetrievalTuning(),
        lexical_anchor_id=6,
    )

    assert [section.id for section in selected] == [1, 2, 3, 4, 6]


def test_retrieval_tuning_is_explicit_and_overrideable():
    tuning = RetrievalTuning(title_weight=3.0, heading_path_weight=2.0, body_weight=1.0)
    assert tuning.title_weight == 3.0
    assert tuning.heading_path_weight == 2.0
    assert tuning.body_weight == 1.0
