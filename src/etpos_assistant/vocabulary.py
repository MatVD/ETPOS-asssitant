from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


CREATE_INTENT_TERMS = (
    "ajouter",
    "creer",
    "cree",
    "creation",
    "nouveau",
    "nouvelle",
    "ouvrir",
    "enregistrer",
    "inserer",
)

INTENT_TERM_GROUPS = (
    ("CREATE", CREATE_INTENT_TERMS),
    ("DIVIDE", ("diviser", "division")),
    ("PRINT", ("imprimer", "impression")),
    ("TRANSFER", ("transferer", "transfert")),
    ("CONFIGURE", ("configurer", "configuration", "regler", "parametrer", "passer", "changer")),
)

ACCOUNT_TERMS = ("compte", "comptes")
USER_TERMS = ("utilisateur", "utilisateurs", "operateur", "operateurs")
TABLE_TERMS = ("table", "tables")
CARD_TERMS = ("carte", "cartes")
CLIENT_TERMS = ("client", "clients")
SUPPLIER_TERMS = ("fournisseur", "fournisseurs")
ARTICLE_TERMS = ("article", "articles")
FAMILY_TERMS = ("famille", "familles")
MODE_TARGET_TERMS = ACCOUNT_TERMS + TABLE_TERMS + CARD_TERMS

OBJECT_TERM_GROUPS = (
    ("COMPTE", ACCOUNT_TERMS),
    ("UTILISATEUR", USER_TERMS),
    ("TABLE", TABLE_TERMS),
    ("CARTE", CARD_TERMS),
    ("CLIENT", CLIENT_TERMS),
    ("FOURNISSEUR", SUPPLIER_TERMS),
    ("ARTICLE", ARTICLE_TERMS),
    ("FAMILLE", FAMILY_TERMS),
)

QUALIFIER_TERM_GROUPS = (
    ("COURANT", ("courant", "courants")),
    ("FOURNISSEUR", ("fournisseur", "fournisseurs")),
    ("MODE", ("mode", "fonctionnement")),
    ("VENTE", ("vente", "ventes")),
)


@dataclass(frozen=True)
class RetrievalConcept:
    key: str
    query_variants: tuple[tuple[str, ...], ...]
    path_signals: tuple[str, ...] = ()
    text_signals: tuple[str, ...] = ()
    negative_signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConceptRule:
    concept: RetrievalConcept
    required_term_groups: tuple[tuple[str, ...], ...] = ()
    any_phrases: tuple[str, ...] = ()
    excluded_phrases: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryAnalysis:
    normalized_question: str
    tokens: tuple[str, ...]
    intents: tuple[str, ...]
    objects: tuple[str, ...]
    qualifiers: tuple[str, ...]
    concepts: tuple[RetrievalConcept, ...]


COMPTE_COURANT = RetrievalConcept(
    key="COMPTE_COURANT",
    query_variants=(
        ("gestion", "compte", "courant"),
        ("compte", "courant"),
        ("comptes", "courants"),
    ),
    text_signals=("gestion des comptes courants", "compte courant", "comptes courants"),
    negative_signals=("reconstruction des comptes courants", "enregistrer le compte", "selectionner famille", "selectionner les articles"),
)

COMPTE_COURANT_CLIENT = RetrievalConcept(
    key="COMPTE_COURANT_CLIENT",
    query_variants=(
        ("compte", "courant", "client"),
        ("comptes", "courants", "clients"),
        ("reglement", "client"),
    ),
    path_signals=("gestion des clients", "comptes courant", "comptes courants"),
    text_signals=("compte courant", "reglement client", "recu"),
    negative_signals=(
        "gestion de fournisseurs",
        "gestionde fournisseurs",
        "reglement fournisseur",
        "liquidation",
        "enregistrer le compte",
        "selectionner famille",
        "selectionner les articles",
    ),
)

COMPTE_COURANT_FOURNISSEUR = RetrievalConcept(
    key="COMPTE_COURANT_FOURNISSEUR",
    query_variants=(
        ("compte", "courant", "fournisseur"),
        ("comptes", "courants", "fournisseurs"),
        ("reglement", "fournisseur"),
        ("liquidation", "fournisseur"),
    ),
    path_signals=("gestion de fournisseurs", "gestion des fournisseurs", "comptes courants des fournisseurs"),
    text_signals=("compte courant", "reglement fournisseur", "liquidation"),
    negative_signals=(
        "gestion des clients",
        "reglement client",
        "creer recu",
        "enregistrer le compte",
        "selectionner famille",
        "selectionner les articles",
    ),
)

COMPTE_VENTE = RetrievalConcept(
    key="COMPTE_VENTE",
    query_variants=(
        ("enregistrer", "compte"),
        ("compte", "famille", "article"),
        ("mode", "comptes"),
    ),
    text_signals=(
        "enregistrer le compte",
        "enregistrer comptes",
        "selectionner compte",
        "mode de fonctionnement",
        "selectionner famille",
        "selectionner les articles",
    ),
    negative_signals=("compte courant", "comptes courants"),
)

UTILISATEUR = RetrievalConcept(
    key="UTILISATEUR",
    query_variants=(
        ("gestion", "utilisateurs"),
        ("fichier", "utilisateurs"),
        ("utilisateur", "modifier"),
    ),
    path_signals=("gestion des utilisateurs", "fichier d utilisateurs"),
    text_signals=("les utilisateurs sont", "fichier utilisateurs", "utilisateur a modifier"),
)

CLIENT_FICHIER = RetrievalConcept(
    key="CLIENT_FICHIER",
    query_variants=(
        ("gestion", "clients", "fichier"),
        ("fichier", "clients"),
        ("modifier", "client"),
    ),
    path_signals=("gestion des clients",),
    text_signals=(
        "fichier de clients",
        "stocke les informations sur les clients",
        "donnees de facturation",
    ),
    negative_signals=("compte courant", "comptes courants", "reglement client"),
)

MODE_FONCTIONNEMENT = RetrievalConcept(
    key="MODE_FONCTIONNEMENT",
    query_variants=(
        ("mode", "fonctionnement", "comptes"),
        ("comptes", "tables", "cartes"),
        ("mode", "tpv"),
    ),
    path_signals=("options a", "modes de fonctionnement"),
    text_signals=("mode de fonctionnement", "comptes tables et cartes", "forme d enregistrement"),
)

ARTICLE = RetrievalConcept(
    key="ARTICLE",
    query_variants=(
        ("gestion", "articles"),
        ("fichier", "articles"),
        ("creer", "articles"),
    ),
    path_signals=("gestion des articles", "fichier d articles"),
    text_signals=("gestion des articles", "fichier d articles", "creer des articles"),
)

FAMILLE = RetrievalConcept(
    key="FAMILLE",
    query_variants=(
        ("gestion", "familles"),
        ("fichier", "familles"),
        ("familles", "articles"),
    ),
    path_signals=("gestion de familles des articles", "fichier des familles"),
    text_signals=("les familles font reference aux articles", "fichier des familles"),
)

REGLEMENT_MIXTE = RetrievalConcept(
    key="REGLEMENT_MIXTE",
    query_variants=(
        ("moyen", "reglement", "mixte"),
        ("plus", "un", "moyen", "reglement"),
    ),
    path_signals=("gestion des reglements", "utiliser un moyen de reglement mixte"),
    text_signals=("plus d un moyen de reglement", "especes et carte"),
)

MODE_BALANCE = RetrievalConcept(
    key="MODE_BALANCE",
    query_variants=(
        ("mode", "balance"),
        ("mode", "fonctionnement", "balance"),
        ("mode", "type", "balance"),
    ),
    path_signals=("balance", "mode balance"),
    text_signals=("mode balance", "mode de fonctionnement de type balance", "mode type de balance"),
)

SAUVEGARDE = RetrievalConcept(
    key="SAUVEGARDE",
    query_variants=(
        ("sauvegarde",),
        ("sauvegarde", "exporter"),
        ("sauvegarde", "automatique"),
    ),
    path_signals=("sauvegarde", "securite et fiabilite"),
    text_signals=("sauvegarde", "sauvegarde automatique", "onglet exporter"),
)

SAUVEGARDE_AUTOMATIQUE = RetrievalConcept(
    key="SAUVEGARDE_AUTOMATIQUE",
    query_variants=(
        ("sauvegarde", "automatique"),
        ("sauvegarde", "planification"),
        ("sauvegarde", "quotidienne"),
    ),
    path_signals=("sauvegarde",),
    text_signals=("sauvegarde automatique", "sauvegardes automatiques"),
)

PERMISSIONS_UTILISATEUR = RetrievalConcept(
    key="PERMISSIONS_UTILISATEUR",
    query_variants=(
        ("definir", "permissions"),
        ("utilisateurs", "permissions"),
        ("fichier", "utilisateurs"),
    ),
    path_signals=("gestion des utilisateurs", "definir les permissions"),
    text_signals=("permissions", "fichier utilisateurs"),
)


CARTE_GENERIQUE = RetrievalConcept(
    key="CARTE_GENERIQUE",
    query_variants=(
        ("fichier", "rfid"),
        ("gestion", "cartes", "consommation"),
    ),
    path_signals=("rfid", "cartes de consommation"),
    text_signals=("fichier de rfid", "gestion des cartes de consommation"),
)


CONCEPT_RULES = (
    ConceptRule(
        concept=SAUVEGARDE,
        any_phrases=("copie de securite", "copies de securite", "backup", "backups"),
    ),
    ConceptRule(
        concept=SAUVEGARDE_AUTOMATIQUE,
        required_term_groups=(
            ("sauvegarde", "sauvegardes", "sauvegarder", "backup", "backups"),
            ("automatique", "automatiquement", "planifier", "programmer", "quotidien", "quotidienne", "nuit", "nuits"),
        ),
    ),
    ConceptRule(
        concept=PERMISSIONS_UTILISATEUR,
        required_term_groups=(
            ("droit", "droits", "permission", "permissions", "limiter"),
            ("utilisateur", "utilisateurs", "operateur", "operateurs", "employe", "employes", "caissier", "caissiers"),
        ),
        any_phrases=("droits utilisateur", "droits des utilisateurs", "definir les permissions"),
    ),
    ConceptRule(
        concept=REGLEMENT_MIXTE,
        any_phrases=(
            "reglement mixte",
            "moyen de reglement mixte",
            "deux moyens de paiement",
            "plusieurs moyens de paiement",
            "paiements differents",
        ),
    ),
    ConceptRule(
        concept=REGLEMENT_MIXTE,
        required_term_groups=(
            ("payer", "paiement", "encaisser", "regler"),
            ("especes", "cash"),
            ("carte", "cb"),
        ),
    ),
    ConceptRule(
        concept=MODE_BALANCE,
        any_phrases=("mode pesee", "mode de pesee", "caisse en mode pesee"),
        required_term_groups=(("pesee", "pesage", "peser"),),
    ),
    ConceptRule(
        concept=CLIENT_FICHIER,
        required_term_groups=(
            CLIENT_TERMS,
            ("fiche", "fichier", "gerer", "gere", "gestion", "modifier", "modifie", "editer"),
        ),
        excluded_phrases=("compte courant", "comptes courants", "reglement client"),
    ),
    ConceptRule(
        concept=ARTICLE,
        required_term_groups=(ARTICLE_TERMS,),
    ),
    ConceptRule(
        concept=FAMILLE,
        required_term_groups=(FAMILY_TERMS,),
    ),
    ConceptRule(
        concept=CARTE_GENERIQUE,
        required_term_groups=(CARD_TERMS, CREATE_INTENT_TERMS),
    ),
    ConceptRule(
        concept=COMPTE_COURANT_CLIENT,
        required_term_groups=(ACCOUNT_TERMS, ("courant", "courants"), CLIENT_TERMS),
        any_phrases=("compte courant client", "comptes courants clients"),
        excluded_phrases=("fournisseur", "fournisseurs"),
    ),
    ConceptRule(
        concept=COMPTE_COURANT_CLIENT,
        required_term_groups=(ACCOUNT_TERMS, CLIENT_TERMS, CREATE_INTENT_TERMS),
        excluded_phrases=("fournisseur", "fournisseurs"),
    ),
    ConceptRule(
        concept=COMPTE_COURANT_FOURNISSEUR,
        required_term_groups=(ACCOUNT_TERMS, ("courant", "courants"), SUPPLIER_TERMS),
        any_phrases=("compte courant fournisseur", "comptes courants fournisseurs"),
        excluded_phrases=("client", "clients"),
    ),
    ConceptRule(
        concept=COMPTE_COURANT,
        required_term_groups=(ACCOUNT_TERMS, ("courant", "courants")),
        any_phrases=("compte courant", "comptes courants"),
        excluded_phrases=("client", "clients", "fournisseur", "fournisseurs"),
    ),
    ConceptRule(
        concept=MODE_FONCTIONNEMENT,
        required_term_groups=(("mode",), MODE_TARGET_TERMS),
    ),
    ConceptRule(
        concept=UTILISATEUR,
        required_term_groups=(USER_TERMS,),
    ),
    ConceptRule(
        concept=UTILISATEUR,
        required_term_groups=(
            ("personne", "personnes", "employe", "employes", "caissier", "caissiers"),
            ("connecter", "connexion", "utiliser", "acces"),
            CREATE_INTENT_TERMS,
        ),
    ),
    ConceptRule(
        concept=COMPTE_VENTE,
        required_term_groups=(ACCOUNT_TERMS, CREATE_INTENT_TERMS),
        any_phrases=("compte de vente", "comptes de vente"),
        excluded_phrases=(
            "compte courant",
            "comptes courants",
            "compte utilisateur",
            "compte d utilisateur",
            "compte operateur",
        ),
    ),
)


def normalize_domain_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.lower())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _rule_matches(rule: ConceptRule, text: str, tokens: set[str]) -> bool:
    if any(phrase in text for phrase in rule.excluded_phrases):
        return False

    phrase_match = any(phrase in text for phrase in rule.any_phrases)
    group_match = bool(rule.required_term_groups) and all(
        any(term in tokens for term in group) for group in rule.required_term_groups
    )
    return phrase_match or group_match


def _matched_labels(
    groups: tuple[tuple[str, tuple[str, ...]], ...],
    tokens: set[str],
) -> tuple[str, ...]:
    return tuple(label for label, terms in groups if any(term in tokens for term in terms))


def analyze_query(question: str) -> QueryAnalysis:
    text = normalize_domain_text(question)
    token_list = tuple(text.split())
    tokens = set(token_list)

    concepts: list[RetrievalConcept] = []
    for rule in CONCEPT_RULES:
        if _rule_matches(rule, text, tokens) and rule.concept not in concepts:
            concepts.append(rule.concept)

    return QueryAnalysis(
        normalized_question=text,
        tokens=token_list,
        intents=_matched_labels(INTENT_TERM_GROUPS, tokens),
        objects=_matched_labels(OBJECT_TERM_GROUPS, tokens),
        qualifiers=_matched_labels(QUALIFIER_TERM_GROUPS, tokens),
        concepts=tuple(concepts),
    )


def detect_retrieval_concepts(question: str) -> tuple[RetrievalConcept, ...]:
    return analyze_query(question).concepts
