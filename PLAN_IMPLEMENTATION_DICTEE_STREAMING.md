# ETPOS Assistant — Plan d'implémentation de la dictée incrémentale

Date de création : 29 septembre 2026.

- Projet local : `/Users/mathieuvandamme/Sites/ETPOS-assistant`.
- Branche de travail : `feat/incremental-dictation`.
- Branche de départ : `main`.
- Commit de référence : `705b57b09c0e4a274c11f805181ab61f7a42241f`.
- Organisation : un commit documentaire initial, puis un commit par étape de développement validée.
- État initial : plan enregistré ; aucune fonctionnalité de streaming implémentée.

Ce document reprend le plan établi dans la conversation, après inspection de la codebase et vérification des références techniques. Il décrit des travaux à réaliser, pas des fonctionnalités déjà disponibles. Les chemins des nouveaux fichiers sont proposés ; toute adaptation devra rester minimale et être consignée ici.

## 1. Objectifs et invariants

### Objectifs d'expérience utilisateur

Afficher un premier texte pendant la dictée, viser un premier texte visible en moins de 2 secondes et un résultat final en moins de 1 à 2 secondes après l'arrêt, lorsque les mesures le permettent. Conserver une transcription finale fiable et limiter le surcoût CPU.

Ces délais sont des cibles, pas des garanties. Une question courte prononcée sans pause est un cas critique : un aperçu peut apparaître plus tôt sans raccourcir la passe finale, voire la retarder s'il occupe le moteur au moment de l'arrêt.

### Socle à préserver

- `faster-whisper`, Whisper `small`, CPU `int8`, français, hotwords ETPOS et VAD Silero.
- Sur le VPS cible : 6 vCPU Intel Haswell, 11 Go de RAM, aucun GPU, `cpu_threads=6`, modèle préchargé, runtime local-only et un worker Uvicorn.
- Une seule inférence Whisper à la fois, commune aux chemins classique et incrémental.
- `POST /api/transcribe` comme chemin classique et solution de repli.
- Aucun changement du RAG, du retrieval, des providers Codex ou du SSE du chat.
- Aucune clé API OpenAI, aucun nouveau moteur, aucune migration de base, aucun nouveau service public.
- Session serveur, CSRF, contrôle d'origine, cookies sécurisés et CSP conservés.
- Aucune persistance des enregistrements dans `app.db` ou `docs.db` ; aucun contenu audio ou transcript dans les journaux opérationnels.
- Aucun accès au VPS, changement Nginx/systemd, déploiement, push ou fusion sans demande explicite distincte.

Références architecturales : `ARCHITECTURE.md`, notamment « Dictée du prompt » et « Performance et observabilité », `SECURITY.md`, ainsi que `ETPOS_Assistant_Architecture.md` dans les sources du projet ChatGPT. Ce plan complète ces documents sans les remplacer.

### Mesure de départ

Les mesures fournies par l'utilisateur sur le VPS pour `small` sont : environ 5,8 secondes d'audio, modèle en cache, 3 876 ms d'itération des segments, 4 066 ms de transcription et 4 107 ms HTTP. Elles n'ont pas été relancées pour la création de ce document.

## 2. Design de référence

```text
Chemin classique conservé
MediaRecorder -> POST /api/transcribe -> transcription finale

Chemin incrémental activable
AudioWorklet -> PCM mono 16 kHz -> WebSocket /api/transcribe/stream
                                         |
                                  buffer audio borné
                                         |
                          ordonnanceur de calculs sérialisés
                                         |
                            modèle Whisper small partagé
                                         |
                         aperçu révisable / résultat terminal
```

Le transport ne déclenche pas une inférence à chaque bloc. Le serveur conserve l'audio, mais remplace les demandes d'aperçu devenues obsolètes.

Le premier chemin incrémental utilisable conservera une finalisation globale. La finalisation aux pauses sera introduite ensuite et comparée à cette référence. La segmentation n'est pas réputée équivalente à une transcription globale tant que sa qualité n'est pas mesurée.

La V1 exclut un décodeur progressif WebM/MP4, une fenêtre glissante avec fusion mot à mot, LocalAgreement comme dépendance, un rééchantillonneur JavaScript maison, Redis et un ordonnanceur multiutilisateur.

## 3. Organisation Git et suivi

### Règles de travail

1. Inspecter Git et relire les fichiers concernés avant chaque étape, via Hermes.
2. Réaliser uniquement le périmètre de l'étape, avec ses tests et sa documentation associée.
3. Vérifier le diff, exécuter les validations pertinentes et corriger avant de commiter.
4. Mettre à jour ce document dans le même commit : cases cochées, tests réellement exécutés, limites et résultats observés.
5. Ajouter explicitement les fichiers concernés à l'index ; ne pas utiliser un ajout global qui pourrait embarquer d'autres travaux.
6. Réaliser un commit par étape terminée. Ne pas lancer les étapes suivantes automatiquement sur la seule base de ce plan.
7. Ne pas réécrire l'historique, pousser, fusionner ni modifier le VPS sans demande explicite.

Une étape implémentée et testée localement ne vaut pas validation des performances sur le VPS. Les validations externes manquantes restent explicitement ouvertes. Un échec bloquant ne doit pas être masqué pour respecter artificiellement le découpage des commits.

### Commits prévus

| Étape | Périmètre | Message de commit | État |
|---|---|---|---|
| 0 | Plan et branche dédiée | `docs: plan incremental dictation implementation` | Plan initial |
| 1 | Moteur commun sur échantillons | `refactor: share Whisper inference for audio samples` | Terminé localement |
| 2 | Benchmark des profils et simulation | `test: benchmark incremental dictation profiles` | Terminé localement ; benchmark VPS à faire |
| 3 | Transport PCM authentifié | `feat: add authenticated PCM dictation transport` | Terminé localement |
| 4 | Ordonnancement borné des aperçus | `feat: schedule bounded dictation previews` | Terminé localement |
| 5 | Capture et interface incrémentales | `feat: add incremental voice dictation UI` | Terminé localement ; validation navigateur restante |
| 6 | Finalisation aux pauses | `feat: finalize dictation at validated speech pauses` | Terminé localement ; activation bloquée avant validation qualité/performance |
| 7 | Préparation de l'activation et du rollback | `chore: prepare streaming dictation rollout and rollback` | Préparation locale terminée ; navigateurs, wheel et VPS à valider |

Les SHA des commits sont consultables dans Git ; ne pas insérer le SHA d'un commit dans le contenu de ce même commit. Le message ci-dessus identifie chaque jalon.

## 4. Étape 0 — Enregistrer le plan

**Objectif :** disposer d'un plan versionné sur une branche isolée, sans modifier l'application.

**Fichier :** `PLAN_IMPLEMENTATION_DICTEE_STREAMING.md`.

- [x] Vérifier le dépôt propre sur `main` et le commit de référence.
- [x] Créer `feat/incremental-dictation` depuis ce commit.
- [x] Définir les sept étapes, leurs fichiers, leurs tests et leurs critères de sortie.

**Validation avant commit :** relire le document, vérifier sa structure, son encodage et le diff ; s'assurer qu'aucun fichier applicatif ni secret n'est inclus. Les tests applicatifs ne sont pas nécessaires pour ce seul ajout documentaire.

**Commit :** `docs: plan incremental dictation implementation`.

## 5. Étape 1 — Extraire le moteur commun sans changer le POST

**Objectif :** préparer l'inférence sur des échantillons PCM sans dupliquer le modèle ni modifier le comportement final existant.

**Fichiers concernés :**

- `src/etpos_assistant/transcription.py`.
- `tests/test_transcription.py`.

### Travaux

- [x] Extraire une fonction interne `transcribe_audio_samples(...)`, ou un nom cohérent avec le module existant.
- [x] Conserver `transcribe_audio_file(...)` comme adaptateur : décodage, validation, appel du moteur commun et métriques du chemin fichier.
- [x] Réutiliser l'instance Whisper, le verrou et les options de chargement existants.
- [x] Introduire des profils internes contrôlés `final` et `preview`, jamais des paramètres arbitraires envoyés par le navigateur.
- [x] Conserver exactement le profil final actuel, notamment `beam_size=5`, français, VAD, hotwords et paramètres implicites de décodage.
- [x] Préparer le profil d'aperçu candidat : `beam_size=1`, `temperature=0.0`, `best_of=1`. Il évite les tentatives à plusieurs températures, mais ne garantit pas un temps maximal.
- [x] Conserver les positions temporelles des segments dans le résultat interne, sans activer les horodatages mot à mot par défaut ni changer la réponse HTTP réussie.
- [x] Permettre un résultat vide normal pour un aperçu silencieux, tout en conservant l'erreur du POST pour un enregistrement entier sans parole.
- [x] Rendre les métriques cohérentes avec le profil réellement utilisé, sans journaliser le texte.

### Validation

- [x] Les tests existants du moteur et du POST restent valides.
- [x] Les arguments du profil final restent identiques à la référence.
- [x] Taille, durée, erreurs de décodage et suppression des fichiers temporaires restent couvertes.
- [x] Les tests prouvent l'absence de deuxième instance de modèle et d'inférences concurrentes.
- [x] Aucun changement des modules RAG/Codex.

**Résultat local du 29 septembre 2026 :**

- `.venv/bin/pytest -q tests/test_transcription.py` : 12 tests réussis.
- `.venv/bin/pytest -q` : 139 tests réussis.
- `git diff --check` : aucune erreur.
- Le contrat HTTP de succès du POST reste `text` + `duration_seconds` ; les segments et le profil restent internes au moteur.
- Aucun benchmark VPS n'est revendiqué à cette étape.

**Critère de sortie :** comportement classique préservé et moteur sur échantillons testable indépendamment du transport.

**Commit :** `refactor: share Whisper inference for audio samples`.

## 6. Étape 2 — Mesurer les profils et simuler les arrivées audio

**Objectif :** choisir une politique d'aperçu sur des mesures plutôt que sur une extrapolation du coût de l'audio complet.

**Fichiers concernés :**

- `src/etpos_assistant/cli.py`, en prolongeant `whisper-benchmark`.
- Nouveau module ciblé proposé : `src/etpos_assistant/transcription_evaluation.py`.
- Tests proposés : `tests/test_transcription_evaluation.py`.
- Description et manifeste du corpus d'évaluation, sans enregistrements privés versionnés.

### Travaux

- [x] Comparer les profils final et aperçu sur des extraits de 1, 2, 4, 6 et 10 secondes.
- [x] Réutiliser les mêmes audios, la même configuration matérielle et les mêmes options pour les comparaisons.
- [x] Séparer chargement du modèle, premier passage d'inférence incluant le premier passage VAD, et exécutions chaudes.
- [x] Enregistrer modèle, profils effectifs, versions des dépendances, SHA Git et caractéristiques utiles de la machine.
- [x] Mesurer temps écoulé et secondes CPU cumulées ; ne pas confondre délai d'inférence et pourcentage CPU.
- [x] Ajouter une simulation d'arrivée des blocs toutes les 250 ms, avec arrêt explicite et moteur réel ou factice injectable.
- [x] Mesurer les hypothèses révisées, le retard de traitement, les appels évités et le coût cumulé par dictée.
- [x] Ajouter des références humaines pour la qualité finale : mots, termes ETPOS, nombres, négations, omissions et répétitions.
- [x] Inclure questions sans pause, pauses nettes, corrections orales, silence, bruit et parole continue longue.
- [x] Garder les audios et rapports contenant des données personnelles hors de Git ; ne versionner que des éléments explicitement non sensibles.

### Validation

- [x] Tests déterministes des métriques et du simulateur, sans téléchargement de modèle ni appel Codex.
- [x] Rapport identifiant explicitement la machine mesurée et n'impliquant aucune équivalence avec la machine cible.
- [x] Médiane, valeurs min/max, appels bruts et nombre d'échantillons publiés ; aucun p95 n'est calculé sur ces petits effectifs.
- [x] Une transcription identique entre plusieurs appels n'est pas assimilée à une transcription correcte.

**Résultat local du 29 septembre 2026 :**

- Le mode historique de `whisper-benchmark` est conservé par défaut ; `--incremental` active l'évaluation dédiée.
- Le mode incrémental décode une seule fois l'audio, mesure séparément le chargement du modèle et un premier passage de chauffe, puis compare `final` et `preview` sur les mêmes fenêtres. L'ordre des deux profils alterne entre répétitions afin de limiter un biais d'ordre systématique.
- Le rapport JSON contient les temps internes Whisper, le temps wall-clock englobant l'appel, les secondes CPU processus, les textes pour revue humaine, le nombre de segments, la configuration Whisper, les versions `faster-whisper` / `ctranslate2`, le SHA Git et les informations matérielles disponibles.
- Le simulateur est purement déterministe : blocs conceptuels de 250 ms par défaut, une seule inférence active, demande d'aperçu en attente remplaçable, arrêt explicite et finalisation prioritaire après la fin d'une inférence déjà active. Il mesure demandes, exécutions, demandes obsolètes, résultats obsolètes, retard audio, coût cumulé, premier résultat/texte et délai après arrêt.
- `eval/transcription_corpus.example.json` décrit les dix scénarios de revue requis sans embarquer d'audio. `eval/transcription_audio/` et `eval/transcription_corpus.local.json` sont ignorés par Git ; `eval/results/*.json` l'était déjà.
- `.venv/bin/pytest -q tests/test_transcription_evaluation.py` : 10 tests réussis.
- `.venv/bin/pytest -q tests/test_transcription.py tests/test_transcription_evaluation.py` : 22 tests réussis.
- `.venv/bin/pytest -q` : 149 tests réussis.
- `git diff --check` : aucune erreur.
- Aucun benchmark Whisper réel n'a été exécuté dans cette étape locale. Aucun résultat du Mac n'est présenté comme représentatif du VPS Intel Haswell, et le VPS n'a pas été consulté.

**Critère de sortie local :** outillage reproductible et testé, commitable sans résultat VPS inventé.

**Jalon de performance distinct :** avant de figer la cadence et d'engager le développement complet des étapes suivantes, mesurer les extraits courts sur le VPS avec autorisation explicite. En l'absence de ces résultats, les gains et paramètres restent provisoires. Aucun accès distant n'est autorisé par ce document seul.

**Commit :** `test: benchmark incremental dictation profiles`.

## 7. Étape 3 — Ajouter le transport PCM sécurisé

**Statut :** terminé localement.

**Objectif :** recevoir une dictée PCM complète via WebSocket et produire une transcription globale finale, sans encore ajouter d'aperçus.

**Fichiers concernés :**

- `src/etpos_assistant/routers/transcription.py`.
- Nouveau module : `src/etpos_assistant/transcription_stream.py`.
- `src/etpos_assistant/main.py` et `src/etpos_assistant/config.py`.
- Helpers de sécurité uniquement si une adaptation ciblée est nécessaire.
- Nouveaux tests WebSocket et adaptations ciblées de `tests/test_transcription.py` et `tests/test_security.py`.

### Protocole minimal

| Message | Contenu et règle |
|---|---|
| Initialisation client | Version, CSRF et déclaration du format ; taille et délai bornés. |
| `ready` serveur | Identifiant de dictée et limites acceptées. |
| Blocs binaires | PCM signé 16 bits little-endian, mono 16 kHz ; séquence et position en échantillons. |
| `finish` client | Dernière séquence et nombre total d'échantillons. |
| `final` serveur | Texte final et couverture de l'audio reçu. |
| `error` serveur | Erreur terminale explicite ; jamais un succès avec audio manquant. |

### Travaux

- [x] Ajouter `/api/transcribe/stream`, désactivé par défaut derrière un réglage serveur.
- [x] Vérifier session et origine exacte ; ne pas supposer que le middleware HTTP protège le WebSocket.
- [x] Exiger le CSRF dans le premier message ; refuser tout audio avant validation et ne placer aucun jeton dans l'URL.
- [x] Tenir compte de l'expiration et de la révocation de session pendant une connexion ouverte.
- [x] Borner messages, file de réception, mémoire, durée audio et délais d'initialisation/inactivité/finalisation.
- [x] Partir de blocs nominaux de 250 ms, soit 8 000 octets de PCM, et d'une limite expérimentale de message de 16 Kio ; conserver la limite globale configurée de 60 secondes par défaut.
- [x] Vérifier l'ordre des séquences, les positions, le format et le total final ; refuser les données après `finish`.
- [x] Mettre en place l'admission commune POST/WebSocket lorsque le streaming est activé : une dictée admise, refus explicite si occupé.
- [x] Conserver la propriété d'un calcul démarré jusqu'à sa fin réelle, même si sa connexion disparaît ; nettoyer l'état sans lancer de nouveau travail.
- [x] Préparer le cycle de vie dans FastAPI, sans modifier le shutdown du provider Codex.
- [x] Produire une finalisation globale avec le profil final de l'étape 1.

### Validation

- [x] Tests d'authentification, origine, CSRF, limites, silence et formats invalides.
- [x] Tests de séquence incorrecte, bloc manquant, fermeture précoce et dernier bloc résiduel.
- [x] Tests d'occupation commune au POST et au WebSocket.
- [x] Aucune fuite d'état ni deuxième inférence après annulation.
- [x] Le contrat de succès du POST reste inchangé ; le nouveau refus si occupé est documenté et testé.

**Résultats locaux :**
- Nouveau transport `/api/transcribe/stream` derrière `ETPOS_WHISPER_STREAMING_ENABLED=false` par défaut.
- Protocole PCM 16 kHz mono s16le avec en-tête binaire séquence + position, contrôle `finish` et couverture finale.
- Session, origine et CSRF validés côté WebSocket ; session revérifiée pendant la réception, avant et après la finalisation.
- Admission commune POST/WebSocket lorsque le streaming est actif ; une inférence lancée conserve l'admission jusqu'à la fin réelle du thread.
- Le nettoyage du fichier temporaire POST suit également le thread d'inférence en cas d'annulation.
- Uvicorn est borné à 16 Kio par message et une file WebSocket de 4 messages dans le Makefile et l'unité systemd d'exemple.
- `.venv/bin/pytest -q` : 169 tests réussis.

**Critère de sortie :** transport complet et sécurisé, avec référence finale globale et feature flag désactivé.

**Commit :** `feat: add authenticated PCM dictation transport`.

## 8. Étape 4 — Ordonnancer des aperçus sans accumuler de calculs

**Statut :** terminé localement ; mesure Whisper sur le VPS cible encore requise.

**Objectif :** anticiper du texte sans saturer le moteur ni allonger inutilement la finalisation.

**Fichiers concernés :** `transcription_stream.py`, intégration limitée dans `main.py`, configuration et tests de concurrence.

### Travaux

- [x] Maintenir une seule inférence active et une seule demande d'aperçu remplaçable.
- [x] Continuer la réception audio pendant l'inférence ; utiliser des instantanés immuables pour les calculs.
- [x] Ne pas retraiter un instantané inchangé ; gérer une hypothèse vide comme un état normal.
- [x] Donner priorité à la finalisation et supprimer l'aperçu en attente à l'arrêt.
- [x] Ne pas promettre d'interrompre le calcul natif déjà démarré ; conserver l'exclusivité jusqu'à sa terminaison réelle.
- [x] Ajouter `partial` avec identifiant de dictée, révision et hypothèse complète remplaçable.
- [x] Ignorer les résultats obsolètes et empêcher leur insertion dans une autre dictée.
- [x] Adapter la cadence aux mesures, sans minuteur alimentant une file illimitée.
- [x] Suspendre les aperçus coûteux sur une portion trop longue, sans abandonner l'audio ni la finalisation globale.
- [x] Exposer les métriques : attente, inférences, demandes remplacées, retard audio et coût par profil.

Paramètres candidats uniquement : premier calcul vers 1 seconde de parole, départs suivants espacés d'au moins 2 à 3 secondes, suspension vers 10 à 12 secondes de portion ouverte. Leur validation dépend du benchmark de l'étape 2.

### Validation

- [x] Faux moteur lent : aucun chevauchement d'inférences et aucune croissance de file d'aperçus.
- [x] Arrêt pendant un calcul : aucun nouvel aperçu ne retarde volontairement la finalisation.
- [x] Déconnexion et shutdown : aucun travail orphelin non suivi.
- [x] Les mises à jour sont remplacées, pas concaténées.
- [ ] Mesure du coût réel Whisper d'une dictée courte sans pause sur le VPS cible, y compris le cas défavorable d'un aperçu en cours à l'arrêt.

**Résultats locaux :**
- `PreviewScheduler` maintient au plus une inférence active et un instantané en attente ; une demande plus récente remplace l'attente précédente.
- La réception WebSocket continue pendant les aperçus. Les instantanés sont des copies immuables du PCM déjà reçu.
- Les résultats rendus obsolètes par une demande plus récente, la suspension ou `finish` ne sont pas envoyés au client.
- `partial` contient `dictation_id`, une révision monotone, l'hypothèse complète et la couverture en échantillons ; une hypothèse vide est valide.
- `finish` supprime l'aperçu en attente, attend seulement le calcul natif déjà lancé, puis exécute la transcription globale avec le profil `final`.
- Cadence provisoire configurable : premier aperçu à 1,0 s d'audio reçu, demandes suivantes espacées de 2,5 s d'audio, suspension après 10,0 s de portion continue. Aucun timer ne produit de file de travaux.
- Les métriques journalisées couvrent demandes, inférences lancées, remplacements, résultats obsolètes, attente, retard audio et coût mural cumulé par profil.
- Tests locaux avec faux moteur lent : une seule inférence simultanée, attente remplaçable bornée, attente de finalisation mesurable et shutdown sans tâche d'inférence orpheline.
- `.venv/bin/pytest -q tests/test_transcription_stream.py` : 24 tests réussis.
- `.venv/bin/pytest -q` : 175 tests réussis.
- `git diff --check` sur les fichiers de l'étape : aucune erreur.
- Aucun benchmark Whisper réel ni accès VPS effectué dans cette étape ; les valeurs 1,0 / 2,5 / 10,0 s restent provisoires.

**Critère de sortie local :** ordonnancement borné et finalisation globale toujours disponible comme référence de qualité.

**Jalon de performance restant :** exécuter le benchmark autorisé sur le VPS avant de considérer la cadence validée pour le déploiement.

**Commit :** `feat: schedule bounded dictation previews`.

## 9. Étape 5 — Intégrer AudioWorklet et l'interface

**Statut :** terminé localement ; validation navigateurs réels et mesures de latence encore requises.

**Objectif :** rendre le chemin incrémental utilisable sans réécrire le frontend ni modifier le chat.

**Fichiers concernés :**

- Nouveaux fichiers proposés : `static/js/voice-worklet.js` et `static/js/voice-stream.js`, sous `src/etpos_assistant/`.
- Raccordement limité dans `static/js/app.js`.
- `templates/chat.html`, `templates/base.html`, `static/css/app.css` et `routers/pages.py`.
- Tests ciblés du contrôleur JavaScript et vérifications navigateur.

### Travaux

- [x] Demander un `AudioContext` à 16 kHz, vérifier sa fréquence et recueillir les échantillons via AudioWorklet.
- [x] Produire du PCM mono signé 16 bits little-endian ; ne pas renvoyer le micro vers les haut-parleurs.
- [x] Utiliser le rééchantillonnage fourni par Web Audio ; ne pas développer de rééchantillonneur maison en V1.
- [x] Choisir le mode classique avant capture si le chemin incrémental est désactivé ou incompatible.
- [x] Ne pas faire tourner systématiquement MediaRecorder en parallèle du worklet.
- [x] Implémenter `idle -> starting -> recording -> finishing -> idle`, avec chemins d'erreur et d'annulation.
- [x] Poser `starting` avant les opérations asynchrones pour empêcher les doubles clics et démarrages concurrents.
- [x] Transmettre les limites depuis le serveur plutôt que conserver une durée frontend indépendante.
- [x] Désactiver l'envoi et le retry du chat pendant la dictée, y compris pendant `starting` et `finishing`.
- [x] Garder le bouton d'arrêt Codex dédié à la génération existante.
- [x] Afficher l'aperçu dans une zone dédiée, via du texte brut ; remplacer chaque révision.
- [x] Appeler `insertTranscription()` une seule fois au résultat final ; aucun envoi automatique au chat.
- [x] Mémoriser texte et sélection au départ ; si la saisie a changé, proposer une insertion explicite plutôt qu'un écrasement silencieux.
- [x] Vider le bloc résiduel du worklet et attendre son accusé de vidage avant l'envoi de `finish`.
- [x] Borner les buffers côté worklet et contrôleur et surveiller `WebSocket.bufferedAmount` ; aucune perte silencieuse de blocs.
- [x] Libérer pistes micro, contexte audio, connexion et timers à la fin ou à l'abandon.
- [x] Ne pas lancer un repli automatique en cas de refus d'authentification, de service occupé ou de coupure en cours de dictée.
- [x] Préserver navigation clavier, messages accessibles et absence de notifications vocales excessives à chaque aperçu.

### Validation

- [x] Tests des états, doubles clics, réponses tardives, refus micro, interruption/backpressure et modification manuelle du texte.
- [x] Vérification du dernier échantillon transmis avant `finish`.
- [x] Contrôle syntaxique JavaScript, puis tests de comportement ; le premier ne remplace pas les seconds.
- [ ] Essai réel sur Brave/Chromium, puis Safari et Firefox, avec vérification du repli classique.
- [ ] Mesures navigateur du premier texte et du délai après arrêt.

**Résultats locaux :**
- Nouveau `voice-worklet.js` : capture mono, conversion float32 vers PCM signé 16 bits little-endian et bloc résiduel vidé avec accusé `flushed`.
- Nouveau `voice-stream.js` : WebSocket validé avant demande micro, état borné `idle/starting/recording/finishing`, limites issues de `ready`, framing séquence/position, backpressure explicite et nettoyage complet.
- Le contrôleur demande `AudioContext({sampleRate: 16000})`, vérifie la fréquence réellement obtenue et utilise un `AudioWorkletNode` sans sortie audio.
- Le feature flag serveur est transmis au template ; si le streaming est désactivé ou incompatible avant capture, le MediaRecorder classique est conservé comme repli.
- Aucun repli automatique n'est lancé après un refus WebSocket, un service occupé, un refus micro, une coupure ou une erreur de backpressure.
- Les `partial` remplacent une zone d'aperçu en texte brut et ne sont jamais injectés dans la question.
- Le résultat `final` est inséré une seule fois si la saisie n'a pas changé ; sinon l'utilisateur obtient un bouton explicite « Insérer la transcription ».
- Envoi et retry du chat sont désactivés pendant la dictée ; le bouton d'arrêt Codex reste réservé à la génération.
- Les limites `max_samples`, taille/message, file et délai de finalisation viennent du serveur. Le contrôleur arrête proprement à la limite d'échantillons sans timer de durée indépendant.
- `node --check` : `app.js`, `voice-stream.js` et `voice-worklet.js` valides.
- `node tests/js/test_voice_stream.js` : tests comportementaux réussis, incluant double démarrage, refus serveur avant micro, refus micro, dernier bloc résiduel, réponse tardive, arrêt vide, limite serveur, backpressure et modification manuelle.
- Compilation Jinja de `base.html` et `chat.html` : réussie.
- `.venv/bin/pytest -q tests/test_chat_ux.py` : 6 tests réussis.
- `.venv/bin/pytest -q tests/test_transcription_stream.py` : 24 tests réussis.
- `.venv/bin/pytest -q` : 175 tests réussis.
- `git diff --check` sur les fichiers de l'étape : aucune erreur.
- Une session Brave locale existe, mais l'accès à `127.0.0.1:8787` est intercepté en 403 avant réponse FastAPI identifiable. L'onglet VPS existant `agent.matblock.com` n'a pas été utilisé. Aucun accès VPS n'a été effectué.

**Critère de sortie local :** intégration incrémentale implémentée et testée sans régression automatisée du compositeur ni du chat.

**Validation restante :** essai réel sur Brave/Chromium, Safari et Firefox puis mesures du premier texte et du délai après arrêt sur une instance contrôlable.

**Commit :** `feat: add incremental voice dictation UI`.

## 10. Étape 6 — Finaliser aux pauses après comparaison de qualité

**Objectif :** réduire le travail restant après l'arrêt sans transformer une hypothèse provisoire en résultat final non vérifié.

**Fichiers concernés :** `transcription_stream.py`, adaptateur VAD ciblé, évaluation et tests des frontières audio.

### Travaux

- [x] Réutiliser Silero derrière un petit adaptateur ; ne pas importer un framework complet de streaming.
- [x] Confirmer un silence effectivement reçu après la parole ; la fin temporaire d'un buffer n'est jamais une preuve de pause.
- [x] Conserver les positions absolues en échantillons et éviter un retraitement systématique de tout l'historique.
- [x] Tester une pause de 500 à 700 ms comme plage initiale, pas comme valeur définitivement validée.
- [x] Regrouper les portions trop courtes pour limiter les appels au modèle.
- [x] Finaliser les portions fermées avec le profil final pendant que le microphone reste ouvert.
- [x] Placer les frontières dans les silences ; ne pas concaténer des fenêtres contenant la même parole.
- [x] Garder ensemble les portions dont la frontière est incertaine plutôt que figer une segmentation douteuse.
- [x] À l'arrêt, réutiliser les portions réellement finalisées et traiter seulement la partie restante lorsque leur couverture est complète.
- [x] Ne pas convertir un aperçu `beam_size=1` en texte final pour améliorer artificiellement une métrique.
- [x] Conserver le mode de référence avec finalisation globale et un moyen de désactiver l'optimisation par portions.
- [x] Pour la parole continue longue, suspendre les aperçus si nécessaire et finaliser la portion complète ; aucun texte tronqué présenté comme intégral.

### Validation

- [ ] Comparaison A/B sur les mêmes audios : finalisation globale contre finalisation par portions.
- [ ] Aucune nouvelle omission ou répétition due aux frontières sur le corpus de validation.
- [ ] Vérification humaine des termes ETPOS, nombres, négations et corrections orales.
- [ ] Comparaison de la qualité, du délai final et du coût CPU, y compris sans pause.
- [ ] Rapports distinguant portions finalisées, aperçu et résultat terminal.

**Résultat local du 30 septembre 2026 :**

- Adaptateur `transcription_vad.py` basé sur l'API VAD de `faster-whisper` 1.2.1, sans dépendance de streaming supplémentaire ni accès à l'état ONNX interne.
- Une fin de buffer n'est jamais considérée comme une pause : une frontière n'est proposée qu'après silence effectivement reçu ou reprise de parole séparée par un silence suffisant.
- Le VAD travaille sur une fenêtre roulante bornée et restitue des positions absolues en échantillons. Les portions finales sont contiguës, non chevauchantes et utilisent exclusivement le profil `FINAL`.
- L'ordonnanceur existant conserve une seule tâche d'inférence suivie ; une frontière finale en attente prend la priorité sur les nouveaux aperçus.
- Les portions trop courtes sont regroupées via `ETPOS_WHISPER_STREAM_MIN_PORTION_SECONDS`, 2,0 s par défaut. Le seuil runtime expérimental est 600 ms.
- `ETPOS_WHISPER_STREAM_PAUSE_FINALIZATION_ENABLED` vaut `false` par défaut. Aucun changement de `.env.example` n'est inclus dans cette étape.
- À l'arrêt, seules les portions effectivement finalisées et contiguës sont réutilisées. Le reste est traité avec `FINAL`; toute incohérence ou erreur de portion provoque un fallback vers la finalisation globale.
- `whisper-benchmark --incremental --pause-finalization` prépare la comparaison A/B sur les mêmes audios et les seuils 500/600/700 ms. Le rapport sépare référence globale, portions finalisées, résultat terminal, coût cumulé, CPU et temps restant après arrêt ; la revue humaine reste explicitement requise pour mots, termes ETPOS, nombres, négations, corrections, omissions et répétitions.
- `.venv/bin/pytest -q tests/test_transcription_stream.py tests/test_transcription_vad.py tests/test_transcription_evaluation.py` : 43 tests réussis.
- `.venv/bin/pytest -q` : 184 tests réussis.
- La validation A/B sur de vrais audios et les mesures sur le VPS cible n'ont pas été exécutées. Les cinq cases de validation ci-dessus restent donc ouvertes et l'optimisation reste désactivée par défaut.

**Critère de sortie local :** implémentation, fallback et outillage A/B terminés et testés sans activer normalement l'optimisation.

**Critère d'activation restant :** une qualité acceptable et un gain mesuré sur la machine cible sont obligatoires ; sinon l'optimisation doit rester désactivée et l'échec être consigné.

**Commit :** `feat: finalize dictation at validated speech pauses`.

## 11. Étape 7 — Préparer l'activation et le rollback

**Objectif :** livrer une fonctionnalité activable, documentée et réversible ; cette étape ne donne pas l'autorisation d'intervenir sur le VPS.

**Fichiers concernés :**

- `ARCHITECTURE.md`, `README.md`, `SECURITY.md` et ce plan.
- `.env.example` uniquement, si accessible et nécessaire ; jamais le vrai `.env`.
- `deploy/nginx/agent.matblock.com.example` et `deploy/systemd/etpos-assistant.service.example`.
- Tests de configuration et de bout en bout.

### Travaux

- [x] Garder le streaming désactivé par défaut jusqu'à validation.
- [x] Documenter paramètres, limites, admission unique et conséquences pour plusieurs utilisateurs.
- [x] Préparer un chemin WebSocket Nginx dédié avec les en-têtes d'upgrade, sans toucher au SSE du chat.
- [x] Choisir et tester explicitement l'implémentation WebSocket Uvicorn avant de fixer ses limites de messages et de file.
- [ ] Vérifier la CSP dans les navigateurs ciblés ; si nécessaire, ajouter uniquement l'origine WebSocket exacte issue d'une configuration serveur validée, jamais un joker.
- [ ] Vérifier que les nouveaux fichiers statiques sont inclus dans un wheel réellement construit.
- [x] Documenter supervision, erreurs attendues et absence de contenu sensible dans les logs.
- [x] Préparer une recette VPS : mesures de référence, activation limitée, contrôles et retour arrière.
- [x] Décrire le rollback : désactiver le streaming, retrouver le POST classique et conserver le même modèle, sans migration.
- [x] Lancer les tests locaux pertinents et contrôler le diff final ; découper les suites si le bridge impose une durée maximale.

### Validation

- [x] Mode classique fonctionnel avec streaming désactivé.
- [x] Tests de sécurité et de cycle de vie réussis.
- [x] Aucun changement non nécessaire du déploiement, du RAG ou de Codex.
- [x] Résultats locaux documentés et cases VPS laissées ouvertes tant qu'elles ne sont pas réellement vérifiées.
- [ ] Autorisation distincte obtenue avant toute modification distante, activation ou push.

**Résultat local du 30 septembre 2026 :**

- Les deux flags `ETPOS_WHISPER_STREAMING_ENABLED` et `ETPOS_WHISPER_STREAM_PAUSE_FINALIZATION_ENABLED` restent `false` par défaut ; `.env.example` n'est pas modifié.
- Uvicorn local est en version 0.49.0 avec `websockets` 17.1. Le chemin legacy `websockets` déclenche des avertissements de dépréciation via `websockets.legacy`. Le rollout sélectionne donc explicitement `websockets-sansio`.
- `websockets-sansio` applique `ws_max_size=16384` et suspend la lecture du transport après remise d'un message jusqu'à sa consommation ASGI. Il n'utilise pas `ws_max_queue` : la borne `max_queue_messages=4` reste une politique applicative annoncée au frontend et appliquée à `WebSocket.bufferedAmount`, pas une prétendue file Uvicorn.
- L'exemple systemd reste à un worker. L'exemple Nginx ajoute uniquement `location = /api/transcribe/stream` avec `Upgrade` / `Connection`, tandis que `location /` reste inchangé pour le chat SSE.
- La CSP locale reste `connect-src 'self'`, sans `ws:`, `wss:` ni joker. Le comportement réel doit encore être vérifié sur Brave/Chromium, Safari et Firefox avant activation.
- La déclaration setuptools `static/js/*.js` et la présence de `voice-stream.js` / `voice-worklet.js` sont testées. La construction d'un wheel réel n'a pas pu être validée dans le sandbox Hermes : le venv ne contient pas `setuptools` et l'isolation de build ne peut pas récupérer `setuptools>=75`.
- `tests/test_streaming_rollout.py` verrouille choix Uvicorn, backpressure Sans-I/O, exemples systemd/Nginx, flags désactivés par défaut et package-data.
- `.venv/bin/pytest -q tests/test_streaming_rollout.py tests/test_security.py tests/test_transcription_stream.py tests/test_transcription.py tests/test_chat_ux.py` : 59 tests réussis.
- `node tests/js/test_voice_stream.js` et les trois `node --check` : réussis.
- `.venv/bin/pytest -q` : 190 tests réussis.
- Aucun accès VPS, push, merge, changement de RAG/Codex ou activation n'a été effectué.

**Critère de sortie local :** préparation locale livrée avec rollback documenté et configuration d'exemple testée. La validation navigateurs, le wheel construit, le déploiement effectif et la recette VPS restent des opérations séparées.

**Commit :** `chore: prepare streaming dictation rollout and rollback`.

## 12. Mesures et critères de décision transversaux

| Dimension | Mesure attendue |
|---|---|
| Premier texte | Temps entre début effectif de capture et premier texte affiché ; aussi depuis le début de parole lorsque cette référence est disponible. |
| Première portion finalisée | Distincte du premier aperçu. |
| Après arrêt | Temps entre clic d'arrêt et résultat terminal affiché, calcul déjà en cours compris. |
| Retard audio | Écart entre audio reçu et audio couvert par les calculs. |
| Coût CPU | Secondes CPU cumulées par dictée et ratio avec le mode classique sur le même audio. |
| Qualité | Erreurs de mots, termes métier, nombres, négations, omissions et répétitions. |
| Ressources | Mémoire, buffers, files et inférences simultanées. |
| Fiabilité | Succès, erreurs, délais dépassés, déconnexions et refus si occupé. |

Les métriques utilisateur sont calculées avec l'horloge monotone du navigateur. Les métriques serveur restent séparées ; ne pas soustraire directement deux horloges de machines différentes. La simulation CLI ne remplace pas les délais d'affichage réels.

Publier médiane, dispersion, effectif et proportion des dictées atteignant les cibles. Ne pas exclure silencieusement les erreurs ou timeouts des rapports.

Budget expérimental proposé : viser un coût CPU cumulé au plus égal à deux fois celui du mode classique sur le corpus de référence. Ce seuil n'est pas une mesure acquise ; son dépassement impose de revoir la cadence ou la stratégie avant activation.

### Jalons encore à valider

- [ ] Profil d'aperçu court mesuré sur le VPS cible.
- [ ] Cadence soutenable sans file de calcul croissante.
- [ ] Premier texte utile mesuré en navigateur.
- [ ] Délai après arrêt mesuré, notamment sur questions sans pause.
- [ ] Qualité finale comparée au mode classique.
- [ ] Surcoût CPU et mémoire acceptables.
- [ ] Compatibilité navigateur et sécurité WebSocket vérifiées.
- [ ] Rollback testé.
- [ ] Autorisation de déploiement/activation obtenue séparément.

## 13. Reprise du travail et journal

Pour reprendre : lire ce document, inspecter `git status`, vérifier la branche et le dernier commit, consulter l'étape non terminée, puis relire ses fichiers réels avant modification. L'état observé via Hermes prévaut sur le plan.

À chaque étape, consigner ici les commandes de test réellement exécutées, leurs résultats, les mesures disponibles et les blocages. Inclure cette mise à jour dans le commit de l'étape ; ne pas marquer une validation externe accomplie sur la seule base de tests locaux.

| Date | Étape | Observation |
|---|---|---|
| 2026-09-29 | 0 | Dépôt propre sur `main`, base `705b57b`, branche `feat/incremental-dictation` créée. Plan initial uniquement ; aucun code applicatif modifié, aucun benchmark VPS relancé. |
| 2026-09-29 | 2 | Outillage local de comparaison `final` / `preview` et simulateur déterministe ajoutés. 10 tests ciblés, 22 tests transcription et 149 tests complets réussis ; `git diff --check` OK. Aucun benchmark Whisper réel ni accès VPS effectué ; les mesures de la machine cible restent ouvertes. |
| 2026-09-29 | 3 | Transport PCM WebSocket sécurisé ajouté derrière feature flag désactivé : origine/session/CSRF, séquences et couverture, limites mémoire/temps, admission commune POST/WebSocket et finalisation globale. Uvicorn limite les messages à 16 Kio et la file à 4. 169 tests complets réussis localement ; aucun accès VPS ni activation du streaming. |
| 2026-09-30 | 4 | Ordonnanceur borné d'aperçus ajouté : une inférence active, une demande remplaçable, réception audio concurrente, résultats obsolètes ignorés, `partial` révisé et finalisation globale prioritaire. 24 tests streaming et 175 tests complets réussis ; aucun benchmark Whisper réel ni accès VPS. La cadence 1,0 / 2,5 / 10,0 s reste provisoire. |
| 2026-09-30 | 5 | AudioWorklet et contrôleur WebSocket intégrés au compositeur avec repli MediaRecorder décidé avant capture, aperçu remplaçable, flush résiduel, backpressure, insertion finale protégée contre les modifications utilisateur et limites fournies par le serveur. Tests JS comportementaux, 24 tests streaming et 175 tests complets réussis. Validation navigateurs réels et mesures de latence encore ouvertes ; aucun accès VPS. |
| 2026-09-30 | 6 | Adaptateur Silero borné, frontières de pause absolues et finalisation `FINAL` de portions contiguës ajoutés derrière flag désactivé, avec fallback global et comparaison A/B 500/600/700 ms. 43 tests ciblés et 184 tests complets réussis. Validation qualité/performance sur vrais audios et VPS encore ouverte. |
| 2026-09-30 | 7 | Préparation locale du rollout : `websockets-sansio` sélectionné explicitement pour Uvicorn 0.49.0, emplacement Nginx WebSocket isolé, systemd maintenu à un worker, sécurité/supervision/rollback documentés et tests de configuration ajoutés. 59 tests ciblés, tests JS/Node et 190 tests complets réussis. CSP navigateurs, wheel construit, activation et recette VPS restent ouvertes ; aucun accès distant ni push. |

## 14. Références techniques du cadrage

Ces références proviennent de la phase d'analyse précédente. Elles expliquent les précautions retenues ; elles ne remplacent ni les tests de la version installée ni les benchmarks du VPS.

- MediaStream Recording, disponibilité et combinaison des blobs : <https://www.w3.org/TR/mediastream-recording/>.
- Web Audio, AudioContext et MediaStreamAudioSourceNode : <https://www.w3.org/TR/webaudio/>.
- faster-whisper 1.2.1, audio et padding : <https://raw.githubusercontent.com/SYSTRAN/faster-whisper/v1.2.1/faster_whisper/audio.py>.
- faster-whisper 1.2.1, options et décodage : <https://raw.githubusercontent.com/SYSTRAN/faster-whisper/v1.2.1/faster_whisper/transcribe.py>.
- faster-whisper 1.2.1, VAD : <https://raw.githubusercontent.com/SYSTRAN/faster-whisper/v1.2.1/faster_whisper/vad.py>.
- Python 3.12, limites d'annulation des futures : <https://docs.python.org/3.12/library/concurrent.futures.html>.
- Starlette, middleware HTTP et WebSocket : <https://www.starlette.dev/middleware/>.
- OWASP, WebSocket Security Cheat Sheet : <https://cheatsheetseries.owasp.org/cheatsheets/WebSocket_Security_Cheat_Sheet.html>.
- Uvicorn, réglages WebSocket : <https://uvicorn.dev/settings/>.
- MDN, WebSocket et bufferedAmount : <https://developer.mozilla.org/en-US/docs/Web/API/WebSocket>.
- MDN, CSP connect-src : <https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/connect-src>.
- Nginx, reverse proxy WebSocket : <https://nginx.org/en/docs/http/websocket.html>.
