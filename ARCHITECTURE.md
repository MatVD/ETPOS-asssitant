# Architecture

## Flux de réponse

```text
Navigateur
  -> POST /api/chat
  -> session + CSRF
  -> recherche FTS5/BM25 locale
  -> top 4-6 sections
  -> provider Codex text-only
       -> exec : codex exec --ephemeral --json --sandbox read-only
       -> ou app-server : processus chaud + thread ephemeral par question
       -> prompt + sources sélectionnées uniquement
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

Les actualités officielles constituent uniquement un niveau documentaire complémentaire pour des fonctionnalités récentes absentes du manuel ou du Support. Elles sont déclarées article par article dans le registre : le pipeline ne parcourt pas automatiquement le blog. La première source active est l'article français ETPOS-Verifone, publié le 4 août 2026, avec une priorité inférieure au Support, un profil `news_article` qui vérifie l'origine `/fr/blog/`, le titre, un volume minimal de contenu et la présence d'une date de publication, ainsi qu'un contrat `eval/news.jsonl`. La date `article:published_time` est conservée comme métadonnée documentaire lorsqu'aucune date de révision n'est disponible. Aucun changement de ranking n'est appliqué uniquement pour faire respecter la hiérarchie des types : une FAQ ou une actualité très précisément pertinente peut légitimement devancer un passage plus général du manuel.

### Retrieval métier

Le retrieval reste fondé sur FTS5/BM25 ; aucun embedding n'est ajouté tant qu'une faiblesse mesurée ne le justifie pas. La requête utilisateur produit un ancrage lexical, puis des variantes issues d'une taxonomie ETPOS contrôlée. Les variantes sont fusionnées par RRF et rerankées avec des signaux de titres, chemins et contenu ; lorsqu'une question active plusieurs concepts plausibles, la sélection finale conserve de la diversité afin qu'un top 5 puisse couvrir plusieurs intentions ou sens métier au lieu d'être monopolisé par un seul concept.

Les fautes de frappe ne déclenchent pas une recherche fuzzy sur tout le corpus. Seuls les tokens suffisamment longs et très proches d'un petit lexique métier contrôlé peuvent être corrigés. La variante lexicale originale est conservée et une variante corrigée est ajoutée ; la correction sert également à la détection des concepts. Cette stratégie vise notamment les erreurs usuelles sur les termes ETPOS (sauvegarde, permissions, utilisateurs, règlements, fournisseurs, etc.) tout en évitant de transformer arbitrairement le vocabulaire libre de l'utilisateur.

La taxonomie contient des concepts réutilisables, pas des réponses codées en dur : comptes courants client/fournisseur, compte de vente, utilisateurs, types et règlements mixtes, sauvegarde manuelle/automatique/restauration, ouverture de caisse, salles/tables, division de compte, promotions par quantité, cartes à points, etc. Toute extension doit être justifiée par un échec reproductible du benchmark et rejouée contre les jeux historiques pour éviter une amélioration locale au prix d'une régression générale.

### Mise à jour de `docs.db`

`docs.db` est reconstructible et pratiquement en lecture seule pendant le fonctionnement normal. Le runtime utilise donc `journal_mode=DELETE` plutôt que WAL afin que l'index actif soit un fichier SQLite autonome, sans sidecars susceptibles de rendre un remplacement atomique ambigu.

La commande `ingest` reste un outil de développement/bootstrap. En production, `update-docs-db` applique le pipeline suivant : téléchargement des seules sources déclarées, comparaison des hashes avec le manifeste actif et comparaison des versions explicites du parser/index, reconstruction complète dans `data/docs-candidates/` dès qu'une source ou une de ces versions change, validation SQLite/FTS, benchmark retrieval de la candidate, puis activation uniquement si les seuils qualité sont satisfaits. Une ancienne `docs.db` sans métadonnées de build reste lisible pour l'historique et les citations, mais elle est reconstruite lors du prochain contrôle de mise à jour.

La candidate est construite sur le même système de fichiers que `docs.db`. Avant activation, l'index actif est préservé dans `data/docs-history/` par hard-link ; `os.replace()` publie ensuite atomiquement la candidate. Les lecteurs ayant déjà ouvert l'ancien inode peuvent finir leur requête, tandis que les nouvelles connexions ouvrent immédiatement le nouvel index. Le rollback suit le même mécanisme dans l'autre sens.

Par défaut `ETPOS_DOCS_HISTORY_KEEP=0`, c'est-à-dire conservation illimitée des versions documentaires. Ce choix vise à préserver la vérifiabilité des réponses historiques. Une rétention positive reste possible si l'exploitation accepte que certaines anciennes citations internes ne soient plus résolubles.

### Stabilité des citations

Un `section_id` SQLite n'est pas une identité documentaire stable : il peut changer après reconstruction de `docs.db`. Les nouvelles citations enregistrent donc notamment l'URL officielle et le hash du document, puis utilisent une route interne `/sources/view` fondée sur ces métadonnées.

La résolution cherche dans la base active et les versions archivées. Pour les anciennes citations créées avant l'ajout du hash documentaire, l'historique est consulté avant la base active afin d'éviter qu'une URL identique ne renvoie silencieusement vers une section modifiée. Le vieux chemin `/sources/{section_id}` reste disponible uniquement pour compatibilité.

## LLM : Codex CLI

Le projet ne passe pas par l'API OpenAI et ne contient aucun `OPENAI_API_KEY`. Codex CLI réutilise l'authentification ChatGPT enregistrée pour le compte système qui exécute FastAPI.

Deux transports sont disponibles derrière le même contrat provider :

- `app-server`, transport par défaut et actuellement déployé en production : un processus `codex app-server --stdio` persiste au niveau du worker FastAPI, mais chaque question crée un thread Codex `ephemeral` distinct. L'historique métier reste donc exclusivement détenu par `app.db`. Son statut expérimental côté Codex est un risque connu et accepté tant que les validations réelles restent satisfaisantes ;
- `exec`, conservé comme solution de rollback immédiat : un `codex exec --ephemeral --json` isolé est lancé pour chaque tour, sans migration de données ni changement du pipeline RAG.

Dans les deux cas, l'historique utile est reconstruit par l'application depuis `app.db` et fourni dans le prompt. La question courante, déjà persistée avant la génération, est retirée de l'historique réinjecté afin de ne pas la dupliquer. Pour les relances courtes, le retrieval peut réutiliser le dernier tour utilisateur pertinent sans second appel LLM. Les anciens marqueurs `[Sx]` sont retirés avant réinjection : seuls les identifiants de sources du tour courant sont citables.

Avec `exec`, le mode JSON expose essentiellement le message assistant terminé : l'interface SSE reçoit donc la réponse en un bloc. Avec `app-server`, le backend consomme `item/agentMessage/delta` uniquement pour l'item `final_answer` et transmet chaque fragment au SSE dès réception. Les raisonnements et autres items ne sont jamais diffusés. Un marqueur interne `full / partial / none`, retiré avant affichage, stabilise la distinction entre réponse complète, réponse partielle et abstention canonique sans coder de réponse métier.

Les deux transports utilisent la même liste de capacités non textuelles désactivées et neutralisent Web ainsi que la configuration MCP locale. `exec` ajoute `--ignore-user-config`, `--ignore-rules`, un répertoire temporaire et un contrôle fail-closed du flux JSON si un item outil apparaît. App Server ajoute des threads `ephemeral`, le refus des demandes d'approbation et le même contrôle fail-closed sur les items outils. Les champs App Server expérimentaux nécessitant une capability explicite ne sont pas envoyés. Le processus App Server est arrêté au shutdown FastAPI et redémarré après erreur, timeout ou annulation. En production, App Server utilise un `ETPOS_CODEX_APP_HOME` dédié ; aucune politique Codex globale n'est imposée au VPS partagé afin de ne pas affecter Hermes ou d'autres usages Codex. `exec` reste le rollback immédiat sans migration de données.

## Performance et observabilité

La performance doit être optimisée à partir de mesures du pipeline réel, sans dégrader la fidélité documentaire. Le chat journalise des métriques structurées sans journaliser le contenu de la question : latence retrieval, génération et finalisation, taille et nombre des passages, taille de l'historique, taille de la réponse et métriques du provider Codex. `eval-answer` conserve en plus, lorsqu'elles sont exposées par Codex, `prompt_chars`, temps de construction du prompt, temps de création du sous-processus, délai jusqu'au premier événement, délai jusqu'au message assistant, `input_tokens`, `cached_input_tokens`, `output_tokens` et `reasoning_output_tokens`.

Les mesures locales du 18 septembre 2026 montrent que FTS5 n'est pas le goulet d'étranglement : le benchmark retrieval à K=5 reste autour de quelques dizaines de millisecondes, tandis que les réponses Codex demandent plusieurs secondes. Le coût de création du processus `exec` est lui-même faible ; l'intérêt principal d'App Server est donc le **time-to-first-text** et non la suppression du spawn. Sur les validations locales, les premiers fragments utiles apparaissent typiquement plusieurs secondes avant la fin de la réponse ; un processus App Server déjà chaud peut également réduire ce délai. Ces mesures restent diagnostiques et ne constituent pas un SLA.

Le prompt demande une réponse concise et directe et utilise un statut interne `full / partial / none` filtré par le provider. Ce protocole permet de conserver le vrai streaming des réponses utiles tout en produisant de façon déterministe l'abstention canonique lorsque les sources n'apportent rien de directement applicable. Les campagnes App Server conservent 100 % d'abstention, de faits obligatoires et de chemins de menus sur Acceptance, ainsi que 100 % de couverture des groupes attendus sur Acceptance, Support et Actualités. Le benchmark principal conserve les faits et chemins attendus ; quelques écarts de couverture de citations déjà identifiés proviennent de sections voisines qui soutiennent effectivement la réponse et restent soumis à revue humaine. Le contexte reste par défaut limité à 6 passages et 9000 caractères par passage.

## Authentification Codex

Local macOS : `codex login`.

VPS headless : `codex login --device-auth` sous le compte Unix dédié au service. Le répertoire d'authentification peut être fixé avec `CODEX_HOME`.

Les identifiants Codex ne doivent jamais se trouver dans le dépôt ou dans `.env`.

## Évaluation qualité

Le benchmark principal est `eval/benchmark.jsonl`. Les attentes sont exprimées avec des termes stables plutôt qu'avec des IDs SQLite, car les IDs peuvent changer après une reconstruction de `docs.db`. `expected_heading_paths` représente l'arborescence du manuel tandis que `expected_menu_paths` est réservé aux vrais chemins de navigation de l'interface ETPOS ; ces deux notions ne doivent pas être confondues.

La première couche d'évaluation est déterministe et porte uniquement sur le retrieval : Recall@K, MRR@K, couverture des groupes de passages attendus, latence moyenne/p95 et détail par catégorie. Les questions déclarées `answerability=none` sont conservées dans le benchmark mais ne sont pas comptées dans le Recall/MRR du retrieval.

La seconde couche, `eval-answer`, utilise le provider Codex et réemploie le même retrieval, le même prompt et la même finalisation de réponse/citations que le chat de production. Elle mesure de manière déterministe : abstention correcte, couverture des faits obligatoires, exactitude des chemins de menus attendus, présence des citations, couverture des groupes de passages attendus par les citations, part des citations situées dans ces passages attendus, et latence de réponse complète. Les faits peuvent déclarer plusieurs formulations textuelles équivalentes et les chemins de menus sont comparés comme des séquences ordonnées d'étapes afin d'éviter les faux négatifs de simple formulation ou mise en forme. La couverture des passages attendus est l'indicateur principal ; la part des citations dans ces passages reste secondaire, car des citations supplémentaires peuvent légitimement soutenir des précisions complémentaires. Aucun de ces indicateurs n'est une mesure sémantique complète du soutien de chaque affirmation par la source. `--docs-db` permet d'exécuter cette évaluation directement contre une candidate non activée. L'abstention complète utilise une réponse canonique afin de pouvoir être scorée sans juge LLM. La commande peut aussi produire un rapport JSON détaillé contenant la réponse, les sections récupérées, les citations, les latences, le modèle, l'effort de raisonnement, la verbosité, le SHA Git et les métriques provider/tokens pour permettre une revue humaine et des comparaisons reproductibles.

Un jeu `eval/acceptance.jsonl`, distinct du benchmark de développement, contient des formulations naturelles qui ont initialement révélé plusieurs lacunes de généralisation du vocabulaire. Après cette première utilisation diagnostique, il est conservé comme suite de régression d'acceptation ; de futurs cas réellement aveugles devront provenir de nouvelles questions terrain et ne pas être utilisés pour régler le retrieval avant leur première mesure. `eval/support.jsonl` sert de contrat de source pour les 10 FAQ françaises officielles. `eval/news.jsonl` sert de contrat de source pour l'actualité Verifone et couvre les trois faits métier principaux retenus avant activation.

Le taux d'hallucination n'est pas calculé automatiquement : il nécessite une revue humaine ou un protocole de juge distinct dont la fiabilité aura été validée. Le déploiement automatique continue donc de ne bloquer que sur l'évaluation retrieval, rapide et déterministe ; l'évaluation Codex peut être exécutée séparément par catégorie ou sur un sous-ensemble de cas.

Baseline locale du corpus actif au 18 septembre 2026, K=5, 47 cas : Recall@5 100,0 %, MRR 0,881, couverture des groupes 100,0 %. Le corpus actif contient 3 documents / 273 sections : manuel ETPOS V5.34, Support français et actualité officielle Verifone. L'acceptance atteint Recall@5 100,0 %, MRR 0,692 et couverture 100,0 %. Le contrat Support atteint Recall@5 100,0 %, MRR 0,773 et couverture 100,0 % sur 10 FAQ ; le contrat Actualités atteint Recall@5 100,0 %, MRR 0,667 et couverture 100,0 % sur 3 cas. Le benchmark couvre explicitement notamment les familles, les périphériques et l'ambiguïté « ouvrir un compte pour un client », pour laquelle les deux sens attendus (compte de vente et compte courant client) sont présents dans le top 5.

## Embeddings

Ils ne font pas partie de la V1. Avant tout ajout, le projet doit mesurer le rappel de FTS5 sur le jeu de questions `eval/`. Une recherche hybride n'est justifiée que si des questions réellement pertinentes échouent lexicalement.
