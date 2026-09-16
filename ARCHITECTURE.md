# Architecture

## Flux de réponse

```text
Navigateur
  -> POST /api/chat
  -> session + CSRF
  -> recherche FTS5/BM25 locale
  -> top 4-6 sections
  -> CodexCliProvider
       -> codex exec --ephemeral --json --sandbox read-only
       -> prompt + sources via stdin
       -> authentification ChatGPT déjà présente dans CODEX_HOME
  -> SSE vers le navigateur
  -> validation des marqueurs [S1]...[Sn]
  -> rendu Markdown filtré côté serveur
  -> persistance du message + citations dans app.db
```

## Stockage

### app.db

Données non reconstructibles : utilisateurs, sessions, conversations, messages.

### docs.db

Données reconstructibles : documents, sections, références d'images et index FTS5.

## Corpus

L'ingestion conserve un snapshot HTML brut dans `snapshots/`, puis produit des sections structurées avec : URL, type de source, priorité, titre, chemin de titres, ancre, version détectée, date de révision détectée, hash et texte exact.

Le texte exact est conservé pour les citations. Un texte normalisé séparé sert à la recherche.

## LLM : Codex CLI

Le projet ne passe pas par l'API OpenAI et ne contient aucun `OPENAI_API_KEY`.

Le backend lance `codex exec` pour chaque tour. Codex CLI réutilise l'authentification ChatGPT enregistrée pour le compte système qui exécute FastAPI.

La commande est volontairement stateless : `--ephemeral`. L'historique utile est reconstruit par l'application depuis `app.db` et fourni dans le prompt. Cela évite de dépendre des fichiers de sessions Codex.

Le mode `--json` produit des événements JSONL. La V1 extrait uniquement les événements `item.completed` de type `agent_message`. Le transport HTTP vers le navigateur reste en SSE, mais Codex CLI ne garantit pas des deltas token-par-token : l'affichage de la réponse peut donc arriver en un bloc final.

Pour éviter qu'une configuration personnelle de Codex modifie le comportement du service, chaque exécution utilise `--ignore-user-config` et `--ignore-rules`, un répertoire temporaire vide et un environnement réduit.

## Authentification Codex

Local macOS : `codex login`.

VPS headless : `codex login --device-auth` sous le compte Unix dédié au service. Le répertoire d'authentification peut être fixé avec `CODEX_HOME`.

Les identifiants Codex ne doivent jamais se trouver dans le dépôt ou dans `.env`.

## Embeddings

Ils ne font pas partie de la V1. Avant tout ajout, le projet doit mesurer le rappel de FTS5 sur le jeu de questions `eval/`. Une recherche hybride n'est justifiée que si des questions réellement pertinentes échouent lexicalement.
