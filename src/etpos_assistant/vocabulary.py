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

ACCOUNT_TERMS = ("compte", "comptes")
USER_TERMS = ("utilisateur", "utilisateurs", "operateur", "operateurs")
MODE_TARGET_TERMS = ("compte", "comptes", "table", "tables", "carte", "cartes")


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


COMPTE_COURANT_CLIENT = RetrievalConcept(
    key="COMPTE_COURANT_CLIENT",
    query_variants=(
        ("compte", "courant", "client"),
        ("comptes", "courants", "clients"),
        ("reglement", "client"),
    ),
    path_signals=("gestion des clients", "comptes courant", "comptes courants"),
    text_signals=("compte courant", "reglement client", "recu"),
    negative_signals=("commerce de detail",),
)

COMPTE_VENTE = RetrievalConcept(
    key="COMPTE_VENTE",
    query_variants=(
        ("enregistrer", "compte"),
        ("commerce", "detail", "compte"),
        ("mode", "comptes"),
    ),
    path_signals=("commerce de detail", "options a"),
    text_signals=("enregistrer le compte", "enregistrer comptes", "mode de fonctionnement"),
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
        required_term_groups=(ACCOUNT_TERMS, ("courant", "courants")),
        any_phrases=("compte courant", "comptes courants"),
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


def detect_retrieval_concepts(question: str) -> tuple[RetrievalConcept, ...]:
    text = normalize_domain_text(question)
    tokens = set(text.split())
    concepts: list[RetrievalConcept] = []
    for rule in CONCEPT_RULES:
        if _rule_matches(rule, text, tokens) and rule.concept not in concepts:
            concepts.append(rule.concept)
    return tuple(concepts)
