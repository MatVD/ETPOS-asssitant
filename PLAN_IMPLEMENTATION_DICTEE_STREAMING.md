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
| 2 | Benchmark des profils et simulation | `test: benchmark incremental dictation profiles` | À faire |
| 3 | Transport PCM authentifié | `feat: add authenticated PCM dictation transport` | À faire |
| 4 | Ordonnancement borné des aperçus | `feat: schedule bounded dictation previews` | À faire |
| 5 | Capture et interface incrémentales | `feat: add incremental voice dictation UI` | À faire |
| 6 | Finalisation aux pauses | `feat: finalize dictation at validated speech pauses` | À faire |
| 7 | Préparation de l'activation et du rollback | `chore: prepare streaming dictation rollout and rollback` | À faire |

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

- [ ] Comparer les profils final et aperçu sur des extraits de 1, 2, 4, 6 et 10 secondes.
- [ ] Réutiliser les mêmes audios, la même configuration matérielle et les mêmes options pour les comparaisons.
- [ ] Séparer chargement du modèle, premier passage VAD et exécutions chaudes.
- [ ] Enregistrer modèle, profils effectifs, versions des dépendances, SHA Git et caractéristiques utiles de la machine.
- [ ] Mesurer temps écoulé et secondes CPU cumulées ; ne pas confondre délai d'inférence et pourcentage CPU.
- [ ] Ajouter une simulation d'arrivée des blocs toutes les 250 ms, avec arrêt explicite et moteur réel ou factice injectable.
- [ ] Mesurer les hypothèses révisées, le retard de traitement, les appels évités et le coût cumulé par dictée.
- [ ] Ajouter des références humaines pour la qualité finale : mots, termes ETPOS, nombres, négations, omissions et répétitions.
- [ ] Inclure questions sans pause, pauses nettes, corrections orales, silence, bruit et parole continue longue.
- [ ] Garder les audios et rapports contenant des données personnelles hors de Git ; ne versionner que des éléments explicitement non sensibles.

### Validation

- [ ] Tests déterministes des métriques et du simulateur, sans téléchargement de modèle ni appel Codex.
- [ ] Rapport distinguant fonctionnement local et performance de la machine cible.
- [ ] Médiane, distribution et nombre d'échantillons publiés ; ne pas présenter un p95 issu de quelques essais comme une mesure robuste.
- [ ] Une transcription identique entre plusieurs appels n'est pas assimilée à une transcription correcte.

**Critère de sortie local :** outillage reproductible et testé, commitable sans résultat VPS inventé.

**Jalon de performance distinct :** avant de figer la cadence et d'engager le développement complet des étapes suivantes, mesurer les extraits courts sur le VPS avec autorisation explicite. En l'absence de ces résultats, les gains et paramètres restent provisoires. Aucun accès distant n'est autorisé par ce document seul.

**Commit :** `test: benchmark incremental dictation profiles`.

## 7. Étape 3 — Ajouter le transport PCM sécurisé

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

- [ ] Ajouter `/api/transcribe/stream`, désactivé par défaut derrière un réglage serveur.
- [ ] Vérifier session et origine exacte ; ne pas supposer que le middleware HTTP protège le WebSocket.
- [ ] Exiger le CSRF dans le premier message ; refuser tout audio avant validation et ne placer aucun jeton dans l'URL.
- [ ] Tenir compte de l'expiration et de la révocation de session pendant une connexion ouverte.
- [ ] Borner messages, file de réception, mémoire, durée audio et délais d'initialisation/inactivité/finalisation.
- [ ] Partir de blocs nominaux de 250 ms, soit 8 000 octets de PCM, et d'une limite expérimentale de message de 16 Kio ; conserver la limite globale configurée de 60 secondes par défaut.
- [ ] Vérifier l'ordre des séquences, les positions, le format et le total final ; refuser les données après `finish`.
- [ ] Mettre en place l'admission commune POST/WebSocket lorsque le streaming est activé : une dictée admise, refus explicite si occupé.
- [ ] Conserver la propriété d'un calcul démarré jusqu'à sa fin réelle, même si sa connexion disparaît ; nettoyer l'état sans lancer de nouveau travail.
- [ ] Préparer le cycle de vie dans FastAPI, sans modifier le shutdown du provider Codex.
- [ ] Produire une finalisation globale avec le profil final de l'étape 1.

### Validation

- [ ] Tests d'authentification, origine, CSRF, limites, silence et formats invalides.
- [ ] Tests de séquence incorrecte, bloc manquant, fermeture précoce et dernier bloc résiduel.
- [ ] Tests d'occupation commune au POST et au WebSocket.
- [ ] Aucune fuite d'état ni deuxième inférence après annulation.
- [ ] Le contrat de succès du POST reste inchangé ; le nouveau refus si occupé est documenté et testé.

**Critère de sortie :** transport complet et sécurisé, avec référence finale globale et feature flag désactivé.

**Commit :** `feat: add authenticated PCM dictation transport`.

## 8. Étape 4 — Ordonnancer des aperçus sans accumuler de calculs

**Objectif :** anticiper du texte sans saturer le moteur ni allonger inutilement la finalisation.

**Fichiers concernés :** `transcription_stream.py`, intégration limitée dans `main.py`, configuration et tests de concurrence.

### Travaux

- [ ] Maintenir une seule inférence active et une seule demande d'aperçu remplaçable.
- [ ] Continuer la réception audio pendant l'inférence ; utiliser des instantanés immuables pour les calculs.
- [ ] Ne pas retraiter un instantané inchangé ; gérer une hypothèse vide comme un état normal.
- [ ] Donner priorité à la finalisation et supprimer l'aperçu en attente à l'arrêt.
- [ ] Ne pas promettre d'interrompre le calcul natif déjà démarré ; conserver l'exclusivité jusqu'à sa terminaison réelle.
- [ ] Ajouter `partial` avec identifiant de dictée, révision et hypothèse complète remplaçable.
- [ ] Ignorer les résultats obsolètes et empêcher leur insertion dans une autre dictée.
- [ ] Adapter la cadence aux mesures, sans minuteur alimentant une file illimitée.
- [ ] Suspendre les aperçus coûteux sur une portion trop longue, sans abandonner l'audio ni la finalisation globale.
- [ ] Exposer les métriques : attente, inférences, demandes remplacées, retard audio et coût par profil.

Paramètres candidats uniquement : premier calcul vers 1 seconde de parole, départs suivants espacés d'au moins 2 à 3 secondes, suspension vers 10 à 12 secondes de portion ouverte. Leur validation dépend du benchmark de l'étape 2.

### Validation

- [ ] Faux moteur lent : aucun chevauchement d'inférences et aucune croissance de file d'aperçus.
- [ ] Arrêt pendant un calcul : aucun nouvel aperçu ne retarde volontairement la finalisation.
- [ ] Déconnexion et shutdown : aucun travail orphelin non suivi.
- [ ] Les mises à jour sont remplacées, pas concaténées.
- [ ] Mesure du coût d'une dictée courte sans pause, y compris le cas défavorable d'un aperçu en cours à l'arrêt.

**Critère de sortie :** ordonnancement borné et finalisation globale toujours disponible comme référence de qualité.

**Commit :** `feat: schedule bounded dictation previews`.

## 9. Étape 5 — Intégrer AudioWorklet et l'interface

**Objectif :** rendre le chemin incrémental utilisable sans réécrire le frontend ni modifier le chat.

**Fichiers concernés :**

- Nouveaux fichiers proposés : `static/js/voice-worklet.js` et `static/js/voice-stream.js`, sous `src/etpos_assistant/`.
- Raccordement limité dans `static/js/app.js`.
- `templates/chat.html`, `templates/base.html`, `static/css/app.css` et `routers/pages.py`.
- Tests ciblés du contrôleur JavaScript et vérifications navigateur.

### Travaux

- [ ] Demander un `AudioContext` à 16 kHz, vérifier sa fréquence et recueillir les échantillons via AudioWorklet.
- [ ] Produire du PCM mono signé 16 bits little-endian ; ne pas renvoyer le micro vers les haut-parleurs.
- [ ] Utiliser le rééchantillonnage fourni par Web Audio ; ne pas développer de rééchantillonneur maison en V1.
- [ ] Choisir le mode classique avant capture si le chemin incrémental est désactivé ou incompatible.
- [ ] Ne pas faire tourner systématiquement MediaRecorder en parallèle du worklet.
- [ ] Implémenter `idle -> starting -> recording -> finishing -> idle`, avec chemins d'erreur et d'annulation.
- [ ] Poser `starting` avant les opérations asynchrones pour empêcher les doubles clics et démarrages concurrents.
- [ ] Transmettre les limites depuis le serveur plutôt que conserver une durée frontend indépendante.
- [ ] Désactiver l'envoi et le retry du chat pendant la dictée, y compris pendant `starting` et `finishing`.
- [ ] Garder le bouton d'arrêt Codex dédié à la génération existante.
- [ ] Afficher l'aperçu dans une zone dédiée, via du texte brut ; remplacer chaque révision.
- [ ] Appeler `insertTranscription()` une seule fois au résultat final ; aucun envoi automatique au chat.
- [ ] Mémoriser texte et sélection au départ ; si la saisie a changé, proposer une insertion explicite plutôt qu'un écrasement silencieux.
- [ ] Vider le bloc résiduel du worklet et attendre son accusé de vidage avant l'envoi de `finish`.
- [ ] Borner les buffers côté worklet et contrôleur et surveiller `WebSocket.bufferedAmount` ; aucune perte silencieuse de blocs.
- [ ] Libérer pistes micro, contexte audio, connexion et timers à la fin ou à l'abandon.
- [ ] Ne pas lancer un repli automatique en cas de refus d'authentification, de service occupé ou de coupure en cours de dictée.
- [ ] Préserver navigation clavier, messages accessibles et absence de notifications vocales excessives à chaque aperçu.

### Validation

- [ ] Tests des états, doubles clics, réponses tardives, refus micro, interruption et modification manuelle du texte.
- [ ] Vérification du dernier échantillon transmis avant `finish`.
- [ ] Contrôle syntaxique JavaScript, puis tests de comportement ; le premier ne remplace pas les seconds.
- [ ] Essai réel sur Brave/Chromium, puis Safari et Firefox, avec vérification du repli classique.
- [ ] Mesures navigateur du premier texte et du délai après arrêt.

**Critère de sortie :** dictée incrémentale expérimentale de bout en bout, sans régression du compositeur ni du chat.

**Commit :** `feat: add incremental voice dictation UI`.

## 10. Étape 6 — Finaliser aux pauses après comparaison de qualité

**Objectif :** réduire le travail restant après l'arrêt sans transformer une hypothèse provisoire en résultat final non vérifié.

**Fichiers concernés :** `transcription_stream.py`, adaptateur VAD ciblé, évaluation et tests des frontières audio.

### Travaux

- [ ] Réutiliser Silero derrière un petit adaptateur ; ne pas importer un framework complet de streaming.
- [ ] Confirmer un silence effectivement reçu après la parole ; la fin temporaire d'un buffer n'est jamais une preuve de pause.
- [ ] Conserver les positions absolues en échantillons et éviter un retraitement systématique de tout l'historique.
- [ ] Tester une pause de 500 à 700 ms comme plage initiale, pas comme valeur définitivement validée.
- [ ] Regrouper les portions trop courtes pour limiter les appels au modèle.
- [ ] Finaliser les portions fermées avec le profil final pendant que le microphone reste ouvert.
- [ ] Placer les frontières dans les silences ; ne pas concaténer des fenêtres contenant la même parole.
- [ ] Garder ensemble les portions dont la frontière est incertaine plutôt que figer une segmentation douteuse.
- [ ] À l'arrêt, réutiliser les portions réellement finalisées et traiter seulement la partie restante lorsque leur couverture est complète.
- [ ] Ne pas convertir un aperçu `beam_size=1` en texte final pour améliorer artificiellement une métrique.
- [ ] Conserver le mode de référence avec finalisation globale et un moyen de désactiver l'optimisation par portions.
- [ ] Pour la parole continue longue, suspendre les aperçus si nécessaire et finaliser la portion complète ; aucun texte tronqué présenté comme intégral.

### Validation

- [ ] Comparaison A/B sur les mêmes audios : finalisation globale contre finalisation par portions.
- [ ] Aucune nouvelle omission ou répétition due aux frontières sur le corpus de validation.
- [ ] Vérification humaine des termes ETPOS, nombres, négations et corrections orales.
- [ ] Comparaison de la qualité, du délai final et du coût CPU, y compris sans pause.
- [ ] Rapports distinguant portions finalisées, aperçu et résultat terminal.

**Critère de sortie :** optimisation implémentée et testée. Son activation normale reste conditionnée à une qualité acceptable et à un gain mesuré sur la machine cible ; sinon elle reste désactivée et son échec est consigné.

**Commit :** `feat: finalize dictation at validated speech pauses`.

## 11. Étape 7 — Préparer l'activation et le rollback

**Objectif :** livrer une fonctionnalité activable, documentée et réversible ; cette étape ne donne pas l'autorisation d'intervenir sur le VPS.

**Fichiers concernés :**

- `ARCHITECTURE.md`, `README.md`, `SECURITY.md` et ce plan.
- `.env.example` uniquement, si accessible et nécessaire ; jamais le vrai `.env`.
- `deploy/nginx/agent.matblock.com.example` et `deploy/systemd/etpos-assistant.service.example`.
- Tests de configuration et de bout en bout.

### Travaux

- [ ] Garder le streaming désactivé par défaut jusqu'à validation.
- [ ] Documenter paramètres, limites, admission unique et conséquences pour plusieurs utilisateurs.
- [ ] Préparer un chemin WebSocket Nginx dédié avec les en-têtes d'upgrade, sans toucher au SSE du chat.
- [ ] Choisir et tester explicitement l'implémentation WebSocket Uvicorn avant de fixer ses limites de messages et de file.
- [ ] Vérifier la CSP dans les navigateurs ciblés ; si nécessaire, ajouter uniquement l'origine WebSocket exacte issue d'une configuration serveur validée, jamais un joker.
- [ ] Vérifier que les nouveaux fichiers statiques sont inclus dans le package distribué.
- [ ] Documenter supervision, erreurs attendues et absence de contenu sensible dans les logs.
- [ ] Préparer une recette VPS : mesures de référence, activation limitée, contrôles et retour arrière.
- [ ] Décrire le rollback : désactiver le streaming, retrouver le POST classique et conserver le même modèle, sans migration.
- [ ] Lancer les tests locaux pertinents et contrôler le diff final ; découper les suites si le bridge impose une durée maximale.

### Validation

- [ ] Mode classique fonctionnel avec streaming désactivé.
- [ ] Tests de sécurité et de cycle de vie réussis.
- [ ] Aucun changement non nécessaire du déploiement, du RAG ou de Codex.
- [ ] Résultats locaux documentés et cases VPS laissées ouvertes tant qu'elles ne sont pas réellement vérifiées.
- [ ] Autorisation distincte obtenue avant toute modification distante, activation ou push.

**Critère de sortie :** préparation locale livrée. Le déploiement effectif et la validation VPS restent des opérations séparées ; commiter un exemple de configuration ne prouve pas qu'il fonctionne en production.

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
