# Sécurité

## Principes applicatifs

- Aucun port public prévu pour l'application : bind loopback en production derrière Nginx.
- Aucun HTML documentaire exécuté.
- Aucun HTML généré par le modèle accepté tel quel.
- Sessions opaques côté serveur ; le cookie ne contient pas d'identité ni de droits.
- Token de session stocké hashé en base.
- Protection CSRF pour les requêtes mutatrices.
- Mots de passe Argon2id.
- Pas d'inscription publique.
- Les URL d'ingestion doivent appartenir à une liste d'hôtes ETPOS autorisés.

## Isolation de Codex CLI

La V1 utilise Codex CLI à la demande du projet, sans clé API.

Chaque appel :

- utilise `codex exec --ephemeral --json` ;
- force `--sandbox read-only` ;
- force `--ask-for-approval never` pour éviter qu'un processus Web reste bloqué sur une question interactive ;
- ignore `config.toml` et les règles utilisateur/projet avec `--ignore-user-config` et `--ignore-rules` ;
- s'exécute dans un répertoire temporaire vide ;
- reçoit uniquement un environnement filtré ;
- reçoit la question et les passages ETPOS par stdin ;
- ne reçoit jamais la base utilisateurs, les cookies, les secrets de l'application ou le chemin du projet dans son prompt.

Le prompt interdit explicitement terminal, fichiers, navigateur, recherche Web, MCP, plugins et connaissances externes.

### Risque résiduel

Codex CLI est un agent capable de commandes locales. Le sandbox `read-only` bloque les écritures mais n'est pas équivalent à « aucun outil ». Une future version destinée à des utilisateurs non fiables devra utiliser un profil de permissions Codex limitant explicitement les lectures à un espace minimal, ou isoler Codex dans un conteneur/service séparé après validation de l'authentification dans cet environnement.

Les identifiants ChatGPT/Codex sont sensibles. `CODEX_HOME` et notamment un éventuel `auth.json` doivent être protégés comme un mot de passe, appartenir uniquement au compte de service et ne jamais être copiés dans le dépôt.

## En-têtes applicatifs

L'application ajoute notamment : CSP restrictive, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy` restrictive et `frame-ancestors 'none'`.

## Production

En production :

- définir `ETPOS_COOKIE_SECURE=true` ;
- placer le service derrière HTTPS/Nginx ;
- utiliser un utilisateur Unix dédié ;
- utiliser un `CODEX_HOME` dédié hors du dépôt ;
- faire `codex login --device-auth` sous cet utilisateur ;
- ne jamais définir `OPENAI_API_KEY` pour cette application ;
- limiter les permissions Unix du répertoire Codex et des bases SQLite.
