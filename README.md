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
# exec = rollback compatible ; app-server = vrai streaming texte
ETPOS_CODEX_TRANSPORT=exec
CODEX_BINARY=codex
CODEX_MODEL=
CODEX_REASONING_EFFORT=
CODEX_MODEL_VERBOSITY=
ETPOS_RETRIEVAL_LIMIT=6
ETPOS_SOURCE_CHAR_LIMIT=9000
CODEX_TIMEOUT_SECONDS=120
```

`ETPOS_CODEX_TRANSPORT=exec` conserve le transport historique et sert de rollback. `ETPOS_CODEX_TRANSPORT=app-server` active le vrai streaming texte via `item/agentMessage/delta`. App Server garde un processus Codex chaud, mais crée un thread éphémère à chaque question : l'historique reste détenu par `app.db`. Laisser `CODEX_MODEL`, `CODEX_REASONING_EFFORT` et `CODEX_MODEL_VERBOSITY` vides conserve les valeurs par défaut de Codex. `ETPOS_RETRIEVAL_LIMIT` et `ETPOS_SOURCE_CHAR_LIMIT` bornent le contexte du chat ; leurs valeurs par défaut restent 6 passages et 9000 caractères par passage tant qu'une réduction n'a pas été validée par les benchmarks. `etpos-assistant codex-status` affiche le transport, le binaire, le répertoire d'authentification effectif et exécute `codex login status` avec le même environnement filtré que le provider de production.

Démarrer :

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

Pour activer App Server en production, utiliser un `CODEX_HOME` **dédié** au transport ETPOS. Ce répertoire est ensuite géré directement par Codex, qui peut y créer ses skills système, caches, bases d'état, logs, snapshots et autres fichiers runtime :

```bash
ETPOS_CODEX_TRANSPORT=app-server
ETPOS_CODEX_APP_HOME=/var/lib/etpos-assistant/codex-app
```

Ce répertoire doit être authentifié directement avec `codex login --device-auth` sous le compte Unix du service. Le backend refuse App Server en production si `ETPOS_CODEX_APP_HOME` n'est pas défini. Repasser `ETPOS_CODEX_TRANSPORT=exec` fournit le rollback immédiat.

## Commandes

```bash
etpos-assistant init-db
etpos-assistant create-user
etpos-assistant ingest
etpos-assistant inspect-source etpos-support-fr
etpos-assistant search "sauvegarde"
etpos-assistant corpus-stats
etpos-assistant codex-status
etpos-assistant backup-app-db
etpos-assistant verify-app-backup /var/lib/etpos-assistant/backups/app-<timestamp>.db
etpos-assistant restore-app-db /var/lib/etpos-assistant/backups/app-<timestamp>.db --target /tmp/app-restore-test.db
etpos-assistant eval-retrieval --path eval/benchmark.jsonl
etpos-assistant eval-retrieval --path eval/acceptance.jsonl
etpos-assistant eval-answer --path eval/benchmark.jsonl --category menu_path --max-cases 5
etpos-assistant eval-answer --path eval/benchmark.jsonl --case-id simple-clients --case-id synonym-customer-record
etpos-assistant eval-answer --path eval/acceptance.jsonl --json-output eval/results/acceptance-<timestamp>.json
etpos-assistant rescore-answer-report --benchmark eval/benchmark.jsonl --report eval/results/benchmark-<timestamp>.json
etpos-assistant update-docs-db --dry-run
etpos-assistant update-docs-db --dry-run --candidate-source etpos-support-fr --validation-benchmark eval/acceptance.jsonl --validation-benchmark eval/support.jsonl
ETPOS_PROVIDER=codex etpos-assistant eval-answer --path eval/support.jsonl --docs-db data/docs-candidates/<candidate>.db --json-output eval/results/support-<timestamp>.json
etpos-assistant docs-history
etpos-assistant rollback-docs-db data/docs-history/docs-<timestamp>-<hash>.db
make quality-baseline
```

`make quality-baseline` est prévu pour être lancé depuis un Terminal normal disposant du vrai `HOME` utilisateur et d'un accès réseau. Il vérifie Codex, inspecte les sources Support et Actualité Verifone sans modifier `docs.db`, rejoue les quatre jeux retrieval (`benchmark`, `acceptance`, contrat Support et contrat Actualités), puis produit les quatre rapports JSON complets des réponses Codex dans `eval/results/`. Il s'arrête dès qu'une étape échoue.

`inspect-source` télécharge et analyse une source déclarée sans modifier `docs.db`. Il peut donc être utilisé sur une source désactivée avant toute activation. La page Support/FAQ française charge actuellement ses réponses côté client : si le HTML initial ne contient que les questions, le pipeline retrouve le bundle `AppBridge` officiel sur le même hôte, en extrait statiquement les Q/R françaises sans exécuter JavaScript et refuse les constructions dynamiques telles que `${...}`. Le hash documentaire combine la page et le bundle afin qu'une modification des réponses déclenche une reconstruction, et le snapshot composite conserve les deux contenus bruts. La validation de l'URL est appliquée à l'URL initiale **et** à l'URL finale après redirection afin qu'une source autorisée ne puisse pas rediriger silencieusement vers un hôte non autorisé. Une source désactivée peut être incluse explicitement dans un `update-docs-db --dry-run` avec `--candidate-source` ; ce flag est refusé hors dry-run afin qu'un test de candidate ne puisse pas activer silencieusement la source. Après validation réelle de l'extraction, du retrieval et des réponses Codex sur candidate, Support est déclaré actif dans le registre. Les benchmarks globaux du registre sont rejoués automatiquement sur toute candidate et `eval/support.jsonl` est en plus attaché à la source Support comme contrat bloquant.

La première actualité officielle active est l'article français consacré à l'intégration ETPOS-Verifone, publié le 4 août 2026. Elle utilise le profil `news_article`, une priorité inférieure au Support et le contrat `eval/news.jsonl`. Son activation a été faite après `inspect-source`, validation retrieval sans régression sur candidate et revue des réponses Codex. Les actualités restent déclarées article par article ; le projet ne crawle pas automatiquement le blog.

## Benchmark qualité ETPOS

Le benchmark principal est `eval/benchmark.jsonl`. Il couvre actuellement 47 cas répartis entre questions simples, procédures, chemins de menus, synonymes, concepts métier ambigus, réponses partielles et questions non répondables. Un second jeu `eval/acceptance.jsonl` utilise des formulations plus naturelles qui n'étaient pas présentes dans le benchmark de développement initial ; il sert désormais de suite de régression d'acceptation après avoir révélé plusieurs lacunes de vocabulaire. La Phase B ajoute `eval/challenge.jsonl`, un jeu diagnostique de 45 formulations réalistes volontairement plus difficiles : langage terrain, fautes de frappe, raccourcis, demandes multi-intentions, ambiguïtés, questions partielles, questions hors documentation et quatre vrais suivis conversationnels avec historique. Ce jeu est conservé comme **holdout diagnostique** : il ne sert pas de seuil bloquant lors des mises à jour documentaires et ses attentes ne doivent pas être modifiées simplement pour améliorer un score. `eval/support.jsonl` est un contrat de source distinct de 10 cas, dérivé des 10 FAQ françaises officielles extraites. `eval/news.jsonl` est le contrat de la source Actualité Verifone et couvre son fonctionnement, sa disponibilité dans la version Light sans module supplémentaire et les modèles location/achat. Ces cinq jeux représentent désormais **120 cas de référence**.

Chaque cas peut déclarer :

- des groupes de sections documentaires attendues ;
- un niveau de réponse `full`, `partial` ou `none` ;
- `expected_heading_paths`, qui décrit l'arborescence du manuel ;
- `expected_menu_paths`, réservé aux vrais chemins de navigation dans l'interface ETPOS ;
- `expected_menu_path_groups` lorsqu'une même procédure possède plusieurs libellés de chemin explicitement documentés ;
- `required_facts` ou `required_fact_groups` pour accepter plusieurs formulations textuelles d'un même fait sans utiliser de juge LLM ;
- un `history` optionnel pour reproduire les vrais suivis conversationnels avec la même construction de requête que le chat de production ;
- une note expliquant les ambiguïtés particulières du cas.

`eval-retrieval` mesure actuellement de façon déterministe :

- Recall@K ;
- MRR@K ;
- couverture des groupes de passages attendus ;
- latence moyenne et p95 du retrieval ;
- détail par catégorie.

Baseline déterministe actuelle du corpus actif au 18 septembre 2026, avec `K=5` et 47 cas : **44 cas répondables**, Recall@5 **100,0 %**, MRR **0,881** et couverture des groupes **100,0 %**. Le corpus contient **3 documents / 273 sections** : manuel ETPOS V5.34, 10 FAQ Support et l'actualité officielle Verifone. Sur `eval/acceptance.jsonl`, **13 cas répondables** obtiennent Recall@5 **100,0 %**, MRR **0,692** et couverture **100,0 %**. Le contrat `eval/support.jsonl` atteint Recall@5 et couverture **100,0 %** sur ses 10 FAQ, avec MRR **0,773** ; `eval/news.jsonl` atteint Recall@5 et couverture **100,0 %** sur ses 3 cas, avec MRR **0,667**. La première mesure du nouveau holdout `eval/challenge.jsonl` donne, sur **41 cas répondables**, Recall@5 **78,0 %**, MRR **0,557** et couverture des groupes **72,0 %**. Les suivis conversationnels atteignent **100 %** de Recall@5 et de couverture, tandis que les fautes de frappe tombent à **42,9 %** et les ambiguïtés réalistes à **60,0 %** de Recall@5 ; les demandes multi-intentions ont Recall@5 **100 %** mais seulement **70,0 %** de couverture des groupes. Ces écarts sont volontairement conservés comme baseline pour orienter les améliorations futures, sans adapter immédiatement le retrieval au jeu challenge. Les cas qui demandent une information actuelle absente de la documentation, mais pour lesquels la documentation fournit tout de même une information ETPOS utile, sont classés `partial` plutôt que `none`.

`eval-answer` constitue la seconde couche. Elle exige `ETPOS_PROVIDER=codex` et réutilise le même retrieval, le même prompt, le même provider et la même finalisation des citations que le chat de production. Elle mesure de façon déterministe l'abstention, la couverture des faits obligatoires, la restitution des vrais chemins de menus ETPOS et la présence des citations. Le matching des faits reste déterministe mais accepte des alternatives déclarées, l'ordre différent de mots significatifs et quelques variantes lexicales contrôlées afin d'éviter des faux négatifs purement rédactionnels. Trois indicateurs de citation sont distingués : la **couverture des passages attendus par les citations**, qui vérifie que chaque groupe documentaire attendu est effectivement cité ; la **part des citations dans les passages attendus**, diagnostic secondaire car une réponse peut légitimement citer des sections supplémentaires ; et la **couverture des faits/chemins présents par les citations**, qui vérifie que les faits obligatoires reconnus dans la réponse sont retrouvables dans un passage cité et que les segments d'un chemin de menu sont supportés par l'ensemble des passages cités. Cette dernière mesure est plus proche du soutien documentaire, mais reste une vérification lexicale déterministe et non un juge sémantique de chaque affirmation libre. Les rapports détaillés conservent aussi les métriques provider exposées par Codex : taille du prompt, temps de spawn, délai jusqu'au premier événement et au message assistant, tokens d'entrée, tokens d'entrée mis en cache, tokens de sortie et tokens de raisonnement. `--docs-db` permet de tester une candidate sans l'activer ; `--case-id`, `--category` et `--max-cases` permettent des campagnes ciblées. `rescore-answer-report` réapplique un benchmark modifié à un rapport JSON existant, en relisant les sections de `docs.db`, sans faire un nouvel appel Codex.

L'abstention complète utilise une phrase canonique afin d'être mesurable sans juge LLM. Les hallucinations ne sont volontairement pas notées automatiquement par `eval-answer` : une revue humaine ou un protocole de juge distinct et validé reste nécessaire avant de publier un taux d'hallucination. Le CI/CD de déploiement continue donc d'exécuter uniquement l'évaluation retrieval, déterministe et rapide.

## Performance du chat

Le retrieval FTS5 est déjà très rapide : la baseline locale à K=5 reste autour de 22 ms de moyenne. Les mesures détaillées montrent que la latence de plusieurs secondes provient presque entièrement de Codex ; le démarrage local du sous-processus ne représente qu'environ 8 à 12 ms sur les appels instrumentés. Le backend journalise donc les temps par phase et les métriques de tokens plutôt que d'optimiser FTS5 sans preuve.

L'interface SSE affiche des états réels pendant l'attente : recherche documentaire, puis génération. Avec le transport `app-server`, elle reçoit ensuite les fragments du `final_answer` au fil de l'eau via `item/agentMessage/delta` ; les raisonnements et événements internes ne sont pas exposés. Un marqueur interne `full / partial / none`, filtré avant affichage, permet de streamer les réponses utiles tout en conservant une abstention canonique déterministe. Les campagnes locales App Server conservent 100 % d'abstention, de faits obligatoires et de chemins de menus sur Acceptance, ainsi que 100 % de couverture des groupes documentaires attendus sur Acceptance, Support et Actualités. Le transport `exec` reste disponible comme fallback.

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
