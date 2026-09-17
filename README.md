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
etpos-assistant backup-app-db
etpos-assistant verify-app-backup /var/lib/etpos-assistant/backups/app-<timestamp>.db
etpos-assistant restore-app-db /var/lib/etpos-assistant/backups/app-<timestamp>.db --target /tmp/app-restore-test.db
etpos-assistant eval-retrieval --path eval/benchmark.jsonl
etpos-assistant eval-answer --path eval/benchmark.jsonl --category menu_path --max-cases 5
etpos-assistant update-docs-db --dry-run
etpos-assistant docs-history
etpos-assistant rollback-docs-db data/docs-history/docs-<timestamp>-<hash>.db
```

## Benchmark qualité ETPOS

Le benchmark principal est `eval/benchmark.jsonl`. Il couvre actuellement 45 cas répartis entre questions simples, procédures, chemins de menus, synonymes, concepts métier ambigus, réponses partielles et questions non répondables.

Chaque cas peut déclarer :

- des groupes de sections documentaires attendues ;
- un niveau de réponse `full`, `partial` ou `none` ;
- `expected_heading_paths`, qui décrit l'arborescence du manuel ;
- `expected_menu_paths`, réservé aux vrais chemins de navigation dans l'interface ETPOS ;
- des faits obligatoires à retrouver dans la réponse finale ;
- une note expliquant les ambiguïtés particulières du cas.

`eval-retrieval` mesure actuellement de façon déterministe :

- Recall@K ;
- MRR@K ;
- couverture des groupes de passages attendus ;
- latence moyenne et p95 du retrieval ;
- détail par catégorie.

Baseline locale du corpus ETPOS V5.34 au 17 septembre 2026, avec `K=5` et 47 cas : Recall@5 **100,0 %**, MRR **0,842**, couverture des groupes **97,8 %**, latence p95 d'environ **51 ms**. Le déficit de couverture restant vient du cas ambigu « ouvrir un compte pour un client », pour lequel un seul des deux sens attendus apparaît dans le top 5.

`eval-answer` constitue la seconde couche. Elle exige `ETPOS_PROVIDER=codex` et réutilise le même retrieval, le même prompt, le même provider et la même finalisation des citations que le chat de production. Elle mesure de façon déterministe l'abstention, la couverture des faits obligatoires, la restitution des vrais chemins de menus ETPOS, la présence et la pertinence des citations, ainsi que la latence complète. `--category` et `--max-cases` permettent des campagnes ciblées sans lancer les 47 appels Codex à chaque fois.

L'abstention complète utilise une phrase canonique afin d'être mesurable sans juge LLM. Les hallucinations ne sont volontairement pas notées automatiquement par `eval-answer` : une revue humaine ou un protocole de juge distinct et validé reste nécessaire avant de publier un taux d'hallucination. Le CI/CD de déploiement continue donc d'exécuter uniquement l'évaluation retrieval, déterministe et rapide.

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

## CI/CD GitHub Actions

Le workflow `.github/workflows/ci.yml` exécute les tests sur les pull requests et les pushes vers `main`. Après un push direct ou fusionné sur `main`, le job `deploy` s'exécute uniquement si les tests ont réussi.

Le déploiement utilise l'environnement GitHub `production` avec :

- variables `VPS_HOST`, `VPS_PORT`, `VPS_USER` ;
- secrets `VPS_SSH_PRIVATE_KEY`, `VPS_SSH_KNOWN_HOSTS`.

Le VPS doit être bootstrapé une première fois avant d'activer le déploiement automatique : dépôt cloné dans `/opt/etpos-assistant`, environnement Python créé, `/etc/etpos-assistant.env` configuré, Codex authentifié, corpus initialisé et service `etpos-assistant.service` fonctionnel.

À chaque déploiement, `deploy/remote-deploy.sh` :

1. refuse un worktree de production modifié ;
2. récupère `origin/main` et vérifie que le SHA cible appartient à `main` ;
3. installe le code et les dépendances du SHA exact ;
4. exécute le benchmark retrieval sur la `docs.db` existante avec les seuils minimaux Recall@5 ≥ 0,95, MRR ≥ 0,80 et couverture des groupes ≥ 0,95 ;
5. redémarre uniquement `etpos-assistant.service` ; le lifespan FastAPI initialise alors les schémas après l'arrêt de l'ancienne instance ;
6. vérifie `/health/ready`, qui exige notamment un corpus documentaire non vide ;
7. revient automatiquement au SHA précédent si le déploiement échoue après le checkout.

L'initialisation de `docs.db` n'est volontairement plus lancée avant le redémarrage : le runtime documentaire passe en `journal_mode=DELETE`, et modifier le mode de journalisation pendant que l'ancienne instance sert encore des lectures créerait une contention inutile.

Les bases SQLite, snapshots, secrets et `CODEX_HOME` ne sont pas déployés par Git.

## Sauvegarde et restauration de `app.db`

`app.db` contient les utilisateurs, sessions et conversations et n'est pas reconstructible. La commande `backup-app-db` utilise l'API de sauvegarde SQLite, valide le fichier obtenu, puis applique la rétention. En production, le répertoire par défaut est `/var/lib/etpos-assistant/backups` et la rétention est de 30 jours. Les variables `ETPOS_BACKUP_DIR` et `ETPOS_BACKUP_RETENTION_DAYS` permettent de les surcharger.

Les exemples systemd `deploy/systemd/etpos-assistant-backup.service.example` et `deploy/systemd/etpos-assistant-backup.timer.example` prévoient quatre sauvegardes quotidiennes. Après installation des unités dans `/etc/systemd/system/`, activer uniquement le timer :

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now etpos-assistant-backup.timer
sudo systemctl list-timers etpos-assistant-backup.timer
```

Tester périodiquement une restauration sans toucher à la production :

```bash
cd /opt/etpos-assistant
sudo -u etpos-assistant .venv/bin/etpos-assistant \
  restore-app-db /var/lib/etpos-assistant/backups/app-<timestamp>.db \
  --target /var/lib/etpos-assistant/restore-test/app.db
```

Puis exécuter `verify-app-backup` sur la base restaurée ou l'ouvrir avec un contrôle applicatif dédié. Pour restaurer réellement `app.db`, arrêter d'abord le service et exécuter la commande sous l'utilisateur `etpos-assistant` :

```bash
sudo systemctl stop etpos-assistant.service
cd /opt/etpos-assistant
sudo -u etpos-assistant .venv/bin/etpos-assistant \
  restore-app-db /var/lib/etpos-assistant/backups/app-<timestamp>.db \
  --confirm-service-stopped
sudo systemctl start etpos-assistant.service
sudo systemctl is-active etpos-assistant.service
```

Une sauvegarde locale sur le même VPS ne couvre pas la perte du serveur lui-même. Une copie hors machine est donc une étape de durcissement distincte.

## Mise à jour atomique de `docs.db`

La commande historique `ingest` reste utile pour le développement et le bootstrap. Elle ne doit pas être utilisée comme mécanisme automatique de mise à jour en production. Le flux de production est `update-docs-db` :

1. télécharger en mémoire uniquement les sources activées dans `config/sources.json` ;
2. comparer leurs hashes avec le manifeste de la base active ;
3. comparer aussi les versions explicites `PARSER_VERSION` et `INDEX_VERSION` stockées dans `build_metadata` ; si les hashes et ces versions sont inchangés, ne rien reconstruire ;
4. si une source, la version du parser ou la version d'index a changé, enregistrer les snapshots si nécessaire et reconstruire entièrement une nouvelle base dans `data/docs-candidates/` ;
5. vérifier l'intégrité SQLite, le schéma, la cohérence de FTS5 et l'absence de sidecars WAL ;
6. exécuter le benchmark retrieval directement contre la candidate ;
7. refuser l'activation si les seuils Recall@5 ≥ 0,95, MRR ≥ 0,80 ou couverture ≥ 0,95 ne sont pas atteints ;
8. archiver l'actuelle `docs.db` par hard-link puis activer la candidate avec `os.replace()` sur le même système de fichiers ;
9. conserver l'ancienne base dans `data/docs-history/` pour rollback et vérification des citations historiques.

Tester tout le pipeline sans activation :

```bash
cd /opt/etpos-assistant
sudo -u etpos-assistant .venv/bin/etpos-assistant update-docs-db --dry-run
```

Puis, après inspection, lancer sans `--dry-run` pour autoriser l'activation. `docs-history` liste les versions archivées et `rollback-docs-db <fichier>` réactive atomiquement une version de l'historique.

`ETPOS_DOCS_HISTORY_KEEP=0` est la valeur par défaut : **0 signifie conserver toutes les versions**. Cette politique est volontaire, car les anciennes conversations doivent rester capables d'ouvrir exactement les passages qui avaient servi à leurs réponses. Une valeur positive peut imposer une rétention limitée, au prix de perdre cette garantie pour les versions supprimées.

Les nouvelles citations stockent le hash du document et utilisent `/sources/view` plutôt qu'un simple ID SQLite. Lors de l'affichage, les anciennes citations sont également normalisées vers cette route stable ; si elles sont antérieures à l'ajout du hash, l'historique documentaire est consulté avant la base courante pour éviter de servir silencieusement une section modifiée.

Les exemples `deploy/systemd/etpos-assistant-docs-update.service.example` et `deploy/systemd/etpos-assistant-docs-update.timer.example` prévoient un contrôle quotidien avec activation seulement après validation. Ils sont fournis comme modèles et **ne sont pas installés ni activés automatiquement**.

Voir aussi `ARCHITECTURE.md`, `SECURITY.md` et `RESEARCH_DECISIONS.md`.
