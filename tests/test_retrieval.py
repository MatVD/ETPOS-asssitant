from pathlib import Path
from types import SimpleNamespace

import etpos_assistant.db as db_module
from etpos_assistant.db import docs_db, init_docs_db
from etpos_assistant.retrieval import build_fts_query, build_search_plan, search_sections


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
