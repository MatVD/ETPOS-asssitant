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

## Évolution validée : transport Codex App Server

Le transport `app-server` est déjà déployé et constitue le transport par défaut du service ETPOS. Il fournit le vrai streaming du texte final sans déplacer le retrieval, les citations ou l'historique métier dans Codex. La documentation Codex actuelle le qualifie encore d'expérimental et non supporté pour les charges de production ; dans ce projet, ce risque est accepté explicitement au regard des bons résultats observés en production, des garde-fous appliqués et de la présence de `exec` comme rollback immédiat.

La décision conserve les frontières architecturales existantes : FastAPI sélectionne les passages ETPOS, construit le contexte et persiste l'historique ; App Server reçoit uniquement le texte nécessaire à un tour. Le processus peut rester chaud, mais chaque question utilise un thread `ephemeral` et un répertoire temporaire distinct.

Cette évolution reste acceptée avec une posture fail-closed : sandbox `read-only`, approbations `never`, recherche Web et capacités outils désactivées, configuration `mcp_servers` explicitement vidée, outils dynamiques/environnements/racines de capacité absents, environnement filtré, refus des demandes de commandes/fichiers et erreur immédiate si un item outil inattendu est observé. Le transport `exec` n'est pas supprimé afin qu'un retour arrière ne nécessite ni migration de données ni modification du pipeline RAG.

## Limite assumée

Codex CLI est un agent de développement et non un simple endpoint de génération de texte. Même en `read-only`, il peut disposer de capacités locales ou gérées selon la version, le compte et la configuration. Pour cette raison, la V1 ajoute une isolation applicative commune aux transports : répertoire de travail temporaire, environnement filtré, sandbox read-only, Web/MCP local et features non textuelles neutralisés, instructions text-only et arrêt fail-closed lorsqu'un item outil apparaît. `exec` ajoute `--ignore-user-config` et `--ignore-rules`. Les politiques MCP gérées au niveau système/organisation ne pouvant pas être isolées par simple `CODEX_HOME`, aucune politique globale supplémentaire n'est imposée au VPS partagé ; la réduction de surface reste donc assurée au niveau du provider ETPOS et de son CODEX_HOME dédié.

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
- Codex App Server : https://developers.openai.com/codex/app-server
- Configuration gérée / exigences Codex : https://developers.openai.com/codex/enterprise/managed-configuration
- Permissions Codex : https://developers.openai.com/codex/permissions
- Python Ubuntu Noble : https://packages.ubuntu.com/noble/python3
- SQLite FTS5 : https://www.sqlite.org/fts5.html
- OWASP Password Storage : https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
