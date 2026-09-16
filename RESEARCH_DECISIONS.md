# Décisions issues de la réanalyse du 16 septembre 2026

## Conservé

1. Python 3.12 : version Python de référence d'Ubuntu 24.04, ce qui limite les différences local/VPS.
2. FastAPI : adapté à une petite application HTTP asynchrone et au transport SSE.
3. SQLite FTS5/BM25 : pertinent pour un corpus ETPOS riche en noms exacts de menus, options, codes et libellés.
4. Deux bases SQLite : `app.db` pour les données utilisateur et `docs.db` pour le corpus reconstructible.
5. RAG léger : pas de LangChain/LlamaIndex ni de base vectorielle tant que les mesures ne le justifient pas.
6. Un seul appel au modèle après récupération locale.
7. Provider abstrait pour que le retrieval, les citations et l'interface ne dépendent pas du mécanisme d'accès au modèle.

## Changement validé : Codex CLI à la place de l'API OpenAI

Le projet n'utilise plus de provider OpenAI API et ne dépend plus du SDK Python `openai`.

Motif : l'objectif est d'utiliser l'accès Codex inclus dans le compte ChatGPT et l'authentification de Codex CLI, sans clé API ni facturation API séparée.

La documentation OpenAI actuelle confirme que :

- Codex CLI accepte `Sign in with ChatGPT` via `codex login` ;
- l'authentification ChatGPT mise en cache est réutilisée par les exécutions suivantes ;
- sur un hôte sans navigateur, `codex login --device-auth` est le chemin recommandé ;
- `codex exec` est le mode non interactif prévu pour scripts et automatisations ;
- `--ephemeral`, `--json`, `--ignore-user-config`, `--ignore-rules` et `--sandbox read-only` sont des options documentées de `codex exec`.

## Limite assumée

Codex CLI est un agent de développement et non un simple endpoint de génération de texte. Même en `read-only`, il peut disposer de capacités de commande locale selon la version et le modèle. Pour cette raison, la V1 ajoute une isolation applicative : répertoire de travail temporaire vide, environnement filtré, config/règles utilisateur ignorées, sandbox read-only et instructions explicites de ne jamais utiliser d'outil.

Pour un assistant documentaire personnel/professionnel protégé par authentification, cette approche est acceptable pour la première version. Avant une ouverture à des utilisateurs non fiables ou à grande échelle, il faudra refaire une revue spécifique de l'isolation Codex et envisager un profil de permissions Codex plus restrictif ou un autre mécanisme d'inférence.

## Vérification du corpus réel avant implémentation

Le manuel français actuel expose une hiérarchie de titres de niveau 1/2/3 pour les grandes rubriques, procédures et sous-procédures. Le parser suit donc cette structure au lieu de découper arbitrairement par nombre de caractères.

Deux protections ont été ajoutées :

- le contenu placé avant le premier titre est ignoré pour éviter d'indexer la table des matières comme une section géante ;
- la date de révision est reconnue sous forme textuelle ou numérique (`30/01/2026`).

## Références techniques vérifiées

- Codex CLI : https://developers.openai.com/codex/cli
- Authentification Codex : https://developers.openai.com/codex/auth
- Référence `codex exec` : https://developers.openai.com/codex/cli/reference
- Permissions Codex : https://developers.openai.com/codex/permissions
- Python Ubuntu Noble : https://packages.ubuntu.com/noble/python3
- SQLite FTS5 : https://www.sqlite.org/fts5.html
- OWASP Password Storage : https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
