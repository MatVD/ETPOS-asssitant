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

Le retrieval combine la requête lexicale d'origine avec des expansions métier contrôlées. Lorsque des expansions sont utilisées, le meilleur résultat de la requête lexicale originale conserve une place dans le top-K : une expansion ne doit pas pouvoir éliminer entièrement la meilleure correspondance aux termes effectivement saisis par l'utilisateur.

## Stockage

### app.db

Données non reconstructibles : utilisateurs, sessions, conversations, messages.

### docs.db

Données reconstructibles : documents, sections, références d'images et index FTS5.

### Sauvegarde de `app.db`

`app.db` contient des données non reconstructibles et fonctionne en mode WAL. Une copie brute du fichier pendant que FastAPI écrit peut donc produire une sauvegarde incohérente ou incomplète.

La sauvegarde applicative utilise exclusivement l'API SQLite `Connection.backup()` vers un fichier temporaire. Le fichier obtenu est contrôlé avec `PRAGMA integrity_check`, `PRAGMA foreign_key_check` et la présence des tables applicatives attendues avant publication par renommage atomique.

En production, les sauvegardes sont stockées par défaut hors du worktree dans `/var/lib/etpos-assistant/backups`. La rétention par défaut est de 30 jours. Le timer systemd prévu exécute quatre sauvegardes quotidiennes ; `Persistent=true` rattrape une occurrence manquée après une indisponibilité du VPS.

La restauration vers une cible distincte peut être testée à chaud. Le remplacement du `app.db` de production exige en revanche l'arrêt préalable de `etpos-assistant.service`, puis une validation de la sauvegarde, une reconstruction dans un fichier temporaire et un remplacement atomique. L'outil CLI demande une confirmation explicite de cet arrêt.

Ces sauvegardes locales protègent contre la corruption applicative et les erreurs de manipulation, mais pas contre la perte complète du VPS. Une copie hors machine devra compléter ce mécanisme lors d'une phase de durcissement de l'exploitation.

## Corpus

L'ingestion conserve un snapshot HTML brut dans `snapshots/`, puis produit des sections structurées avec : URL, type de source, priorité, titre, chemin de titres, ancre, version détectée, date de révision détectée, hash et texte exact. La base documentaire stocke aussi des métadonnées de build avec une `PARSER_VERSION` et une `INDEX_VERSION` explicites.

Le texte exact est conservé pour les citations. Un texte normalisé séparé sert à la recherche.

Les sources ne sont pas considérées comme exploitables uniquement parce qu'elles sont officielles et téléchargeables. Elles peuvent déclarer un profil de validation de contenu appliqué avant parsing/indexation. Pour la source Support/FAQ française, le HTML initial peut ne contenir que les questions alors que les réponses sont embarquées dans le bundle client `AppBridge`. Le pipeline peut alors récupérer ce bundle uniquement sur le même hôte officiel, extraire statiquement les Q/R françaises sans exécuter JavaScript, refuser toute interpolation dynamique et construire une section documentaire par Q/R. Le hash documentaire combine page HTML, URL du composant et bundle afin de détecter les changements de réponses ; un snapshot composite conserve les contenus bruts. `inspect-source` permet de tester ce contrat sans modifier `docs.db`. Une source encore désactivée peut être injectée explicitement dans une candidate avec `update-docs-db --dry-run --candidate-source <id>` ; ce mécanisme est refusé hors dry-run afin de séparer strictement validation et activation. Après validation de l'extraction réelle, des benchmarks retrieval et des réponses Codex sur candidate, la source Support peut être activée. Le registre déclare alors `eval/support.jsonl` comme contrat de source : ce benchmark est automatiquement rejoué sur toute candidate contenant Support, en complément des benchmarks globaux du registre. Les URL sont validées avant la requête et après toute redirection HTTP.

Les actualités officielles constituent uniquement un niveau documentaire complémentaire pour des fonctionnalités récentes absentes du manuel ou du Support. Elles sont déclarées article par article dans le registre : le pipeline ne parcourt pas automatiquement le blog. Un pilote désactivé couvre l'article français ETPOS-Verifone avec une priorité inférieure au Support, un profil `news_article` qui vérifie l'origine `/fr/blog/`, le titre, un volume minimal de contenu et la présence d'une date de publication, ainsi qu'un contrat `eval/news.jsonl`. Ce jeu n'entre dans les validations automatiques que lorsque la source est explicitement injectée dans une candidate ou activée. Aucun changement de ranking n'est appliqué uniquement pour faire respecter la hiérarchie des types : une FAQ ou une actualité très précisément pertinente peut légitimement devancer un passage plus général du manuel.

### Mise à jour de `docs.db`

`docs.db` est reconstructible et pratiquement en lecture seule pendant le fonctionnement normal. Le runtime utilise donc `journal_mode=DELETE` plutôt que WAL afin que l'index actif soit un fichier SQLite autonome, sans sidecars susceptibles de rendre un remplacement atomique ambigu.

La commande `ingest` reste un outil de développement/bootstrap. En production, `update-docs-db` applique le pipeline suivant : téléchargement des seules sources déclarées, comparaison des hashes avec le manifeste actif et comparaison des versions explicites du parser/index, reconstruction complète dans `data/docs-candidates/` dès qu'une source ou une de ces versions change, validation SQLite/FTS, benchmark retrieval de la candidate, puis activation uniquement si les seuils qualité sont satisfaits. Une ancienne `docs.db` sans métadonnées de build reste lisible pour l'historique et les citations, mais elle est reconstruite lors du prochain contrôle de mise à jour.

La candidate est construite sur le même système de fichiers que `docs.db`. Avant activation, l'index actif est préservé dans `data/docs-history/` par hard-link ; `os.replace()` publie ensuite atomiquement la candidate. Les lecteurs ayant déjà ouvert l'ancien inode peuvent finir leur requête, tandis que les nouvelles connexions ouvrent immédiatement le nouvel index. Le rollback suit le même mécanisme dans l'autre sens.

Par défaut `ETPOS_DOCS_HISTORY_KEEP=0`, c'est-à-dire conservation illimitée des versions documentaires. Ce choix vise à préserver la vérifiabilité des réponses historiques. Une rétention positive reste possible si l'exploitation accepte que certaines anciennes citations internes ne soient plus résolubles.

### Stabilité des citations

Un `section_id` SQLite n'est pas une identité documentaire stable : il peut changer après reconstruction de `docs.db`. Les nouvelles citations enregistrent donc notamment l'URL officielle et le hash du document, puis utilisent une route interne `/sources/view` fondée sur ces métadonnées.

La résolution cherche dans la base active et les versions archivées. Pour les anciennes citations créées avant l'ajout du hash documentaire, l'historique est consulté avant la base active afin d'éviter qu'une URL identique ne renvoie silencieusement vers une section modifiée. Le vieux chemin `/sources/{section_id}` reste disponible uniquement pour compatibilité.

## LLM : Codex CLI

Le projet ne passe pas par l'API OpenAI et ne contient aucun `OPENAI_API_KEY`.

Le backend lance `codex exec` pour chaque tour. Codex CLI réutilise l'authentification ChatGPT enregistrée pour le compte système qui exécute FastAPI.

La commande est volontairement stateless : `--ephemeral`. L'historique utile est reconstruit par l'application depuis `app.db` et fourni dans le prompt. Cela évite de dépendre des fichiers de sessions Codex. Pour les relances courtes qui ne portent pas à elles seules un concept métier suffisant, le retrieval peut réutiliser le dernier tour utilisateur pertinent afin de résoudre le contexte sans second appel LLM. Les marqueurs `[Sx]` des anciennes réponses assistant sont supprimés avant réinjection dans le prompt : seuls les identifiants de sources du tour courant peuvent être cités.

Le mode `--json` produit des événements JSONL. La V1 extrait uniquement les événements `item.completed` de type `agent_message`. Le transport HTTP vers le navigateur reste en SSE, mais Codex CLI ne garantit pas des deltas token-par-token : l'affichage de la réponse peut donc arriver en un bloc final.

Pour éviter qu'une configuration personnelle de Codex modifie le comportement du service, chaque exécution utilise `--ignore-user-config` et `--ignore-rules`, un répertoire temporaire vide et un environnement réduit.

## Authentification Codex

Local macOS : `codex login`.

VPS headless : `codex login --device-auth` sous le compte Unix dédié au service. Le répertoire d'authentification peut être fixé avec `CODEX_HOME`.

Les identifiants Codex ne doivent jamais se trouver dans le dépôt ou dans `.env`.

## Évaluation qualité

Le benchmark principal est `eval/benchmark.jsonl`. Les attentes sont exprimées avec des termes stables plutôt qu'avec des IDs SQLite, car les IDs peuvent changer après une reconstruction de `docs.db`. `expected_heading_paths` représente l'arborescence du manuel tandis que `expected_menu_paths` est réservé aux vrais chemins de navigation de l'interface ETPOS ; ces deux notions ne doivent pas être confondues.

La première couche d'évaluation est déterministe et porte uniquement sur le retrieval : Recall@K, MRR@K, couverture des groupes de passages attendus, latence moyenne/p95 et détail par catégorie. Les questions déclarées `answerability=none` sont conservées dans le benchmark mais ne sont pas comptées dans le Recall/MRR du retrieval.

La seconde couche, `eval-answer`, utilise le provider Codex et réemploie le même retrieval, le même prompt et la même finalisation de réponse/citations que le chat de production. Elle mesure de manière déterministe : abstention correcte, couverture des faits obligatoires, exactitude des chemins de menus attendus, présence des citations, couverture des groupes de passages attendus par les citations, part des citations situées dans ces passages attendus, et latence de réponse complète. Les faits peuvent déclarer plusieurs formulations textuelles équivalentes et les chemins de menus sont comparés comme des séquences ordonnées d'étapes afin d'éviter les faux négatifs de simple formulation ou mise en forme. La couverture des passages attendus est l'indicateur principal ; la part des citations dans ces passages reste secondaire, car des citations supplémentaires peuvent légitimement soutenir des précisions complémentaires. Aucun de ces indicateurs n'est une mesure sémantique complète du soutien de chaque affirmation par la source. `--docs-db` permet d'exécuter cette évaluation directement contre une candidate non activée. L'abstention complète utilise une réponse canonique afin de pouvoir être scorée sans juge LLM. La commande peut aussi produire un rapport JSON détaillé contenant la réponse, les sections récupérées, les citations, les latences, le modèle configuré et le SHA Git pour permettre une revue humaine et des comparaisons reproductibles.

Un jeu `eval/acceptance.jsonl`, distinct du benchmark de développement, contient des formulations naturelles qui ont initialement révélé plusieurs lacunes de généralisation du vocabulaire. Après cette première utilisation diagnostique, il est conservé comme suite de régression d'acceptation ; de futurs cas réellement aveugles devront provenir de nouvelles questions terrain et ne pas être utilisés pour régler le retrieval avant leur première mesure. Un troisième jeu `eval/support.jsonl` sert de contrat de source : il couvre chacune des 10 FAQ françaises officielles extraites de la page Support et vérifie qu'une candidate qui inclut cette source les retrouve effectivement avant activation.

Le taux d'hallucination n'est pas calculé automatiquement : il nécessite une revue humaine ou un protocole de juge distinct dont la fiabilité aura été validée. Le déploiement automatique continue donc de ne bloquer que sur l'évaluation retrieval, rapide et déterministe ; l'évaluation Codex peut être exécutée séparément par catégorie ou sur un sous-ensemble de cas.

Baseline locale ETPOS V5.34 au 17 septembre 2026, K=5, 47 cas : Recall@5 100,0 %, MRR 0,876, couverture des groupes 100,0 %. L'acceptance atteint Recall@5 100,0 %, MRR 0,699 et couverture 100,0 %. Une candidate Manuel + Support conserve Recall@5 et couverture à 100,0 % sur ces deux jeux (MRR 0,863 et 0,699) ; le contrat de source Support atteint Recall@5 100,0 %, MRR 0,773 et couverture 100,0 % sur les 10 FAQ. Le benchmark couvre explicitement notamment les familles, les périphériques et l'ambiguïté « ouvrir un compte pour un client », pour laquelle les deux sens attendus (compte de vente et compte courant client) sont présents dans le top 5.

## Embeddings

Ils ne font pas partie de la V1. Avant tout ajout, le projet doit mesurer le rappel de FTS5 sur le jeu de questions `eval/`. Une recherche hybride n'est justifiée que si des questions réellement pertinentes échouent lexicalement.
