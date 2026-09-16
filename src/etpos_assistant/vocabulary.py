from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


CREATE_INTENT_TERMS = (
    "ajouter",
    "creer",
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
MODE_TARGET_TERMS = ACCOUNT_TERMS + TABLE_TERMS + CARD_TERMS

OBJECT_TERM_GROUPS = (
    ("COMPTE", ACCOUNT_TERMS),
    ("UTILISATEUR", USER_TERMS),
    ("TABLE", TABLE_TERMS),
    ("CARTE", CARD_TERMS),
    ("CLIENT", CLIENT_TERMS),
    ("FOURNISSEUR", SUPPLIER_TERMS),
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


CONCEPT_RULES = (
    ConceptRule(
        concept=COMPTE_COURANT_CLIENT,
        required_term_groups=(ACCOUNT_TERMS, ("courant", "courants"), CLIENT_TERMS),
        any_phrases=("compte courant client", "comptes courants clients"),
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
