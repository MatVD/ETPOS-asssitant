# ETPOS Assistant

Assistant documentaire local-first dédié à la documentation officielle ETPOS.

## Objectif

Le projet répond à des questions en français à partir d'un corpus ETPOS indexé localement. Le chemin nominal est volontairement court :

`question -> SQLite FTS5/BM25 -> 4 à 6 sections -> Codex CLI -> réponse + citations`

Le projet **n'utilise pas de clé OpenAI API**. Le provider de production prévu est Codex CLI, authentifié avec le compte ChatGPT de l'utilisateur ou du compte Linux de service.

## Choix de V1

- Python 3.12, compatible avec Ubuntu 24.04.
- FastAPI pour l'application HTTP.
- HTML/CSS/JavaScript léger, sans SPA ni Node en production.
- SQLite séparé en `app.db` et `docs.db`.
- FTS5/BM25 comme moteur de recherche initial.
- Pas d'embeddings tant qu'un jeu d'évaluation ne démontre pas leur utilité.
- Sessions serveur opaques et mots de passe Argon2id.
- Transport SSE entre le backend et le navigateur.
- Providers : `mock` pour le diagnostic local et `codex` pour Codex CLI.
- Citations sélectionnées et validées côté serveur.

## Pourquoi Codex CLI

Codex CLI sait réutiliser une connexion ChatGPT et `codex exec` est le mode non interactif prévu pour les scripts. L'application l'appelle comme sous-processus, sans clé API.

Chaque réponse Codex est lancée avec les protections suivantes :

- exécution `--ephemeral` ;
- JSONL machine-readable ;
- sandbox `read-only` ;
- aucune demande d'approbation interactive ;
- `--ignore-user-config` ;
- `--ignore-rules` ;
- répertoire de travail temporaire vide ;
- environnement du sous-processus réduit à une liste minimale ;
- prompt interdisant terminal, fichiers, Web, MCP, plugins et connaissances externes ;
- seules les sections ETPOS récupérées localement sont injectées dans le prompt.

Important : Codex CLI reste un agent capable de commandes locales. Le sandbox et l'isolation réduisent ce risque mais ne transforment pas Codex en simple endpoint de complétion sans outils. Voir `SECURITY.md`.

## Démarrage local sur macOS

Prérequis : Python 3.12 et Codex CLI installés.

Installer Codex CLI si nécessaire :

```bash
brew install --cask codex
```

ou utiliser l'installeur officiel OpenAI.

Puis authentifier Codex avec ChatGPT :

```bash
codex login
codex login status
```

Installer ensuite ETPOS Assistant :

```bash
cp .env.example .env
make install
make init-db
make create-user
make ingest
```

Tester d'abord le retrieval sans LLM :

```bash
ETPOS_PROVIDER=mock make dev
```

Puis activer Codex dans `.env` :

```bash
ETPOS_PROVIDER=codex
CODEX_BINARY=codex
CODEX_MODEL=
CODEX_TIMEOUT_SECONDS=120
```

Laisser `CODEX_MODEL` vide utilise le modèle par défaut de Codex. Démarrer :

```bash
make dev
```

Puis ouvrir : http://127.0.0.1:8787

`make ingest` télécharge uniquement les sources activées dans `config/sources.json`. Le téléchargement n'a lieu qu'à l'ingestion, jamais à chaque question utilisateur.

## VPS sans navigateur

Le futur service Linux utilisera son propre compte Unix et son propre `CODEX_HOME`. Pour une machine headless, OpenAI prévoit l'authentification par code appareil :

```bash
sudo -u etpos-assistant \
  CODEX_HOME=/var/lib/etpos-assistant/codex \
  codex login --device-auth
```

L'authentification doit être faite une fois avant de lancer le service. Aucun `OPENAI_API_KEY` n'est nécessaire.

## Commandes

```bash
etpos-assistant init-db
etpos-assistant create-user
etpos-assistant ingest
etpos-assistant search "sauvegarde"
etpos-assistant corpus-stats
etpos-assistant codex-status
etpos-assistant eval-retrieval --path eval/questions.example.jsonl
```

## Structure

```text
src/etpos_assistant/
  main.py              application FastAPI
  config.py            configuration par variables d'environnement
  db.py                app.db et docs.db
  security.py          Argon2id, sessions, CSRF
  retrieval.py         FTS5/BM25
  rag.py               orchestration retrieval -> Codex -> citations
  providers/
    mock.py            diagnostic sans LLM
    codex_cli.py       intégration codex exec
    prompting.py       prompt documentaire commun
  ingestion/           récupération, parsing et indexation des sources
  routers/             routes HTTP
  templates/           interface HTML
  static/              CSS et JS
```

Voir aussi `ARCHITECTURE.md`, `SECURITY.md` et `RESEARCH_DECISIONS.md`.
