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

La V1 utilise Codex CLI sans clé API. Deux transports implémentent le même contrat de génération textuelle : `exec`, conservé comme rollback, et `app-server`, utilisé lorsque le vrai streaming texte est souhaité.

### Transport `exec`

Chaque tour :

- utilise `codex exec --ephemeral --json` ;
- force `--sandbox read-only` et `--ask-for-approval never` ;
- ignore `config.toml` et les règles utilisateur/projet avec `--ignore-user-config` et `--ignore-rules` ;
- désactive explicitement Web, shell tools, navigateur/computer use, plugins, skills, mémoires, multi-agent et les features MCP configurables avec la même liste que le transport App Server ;
- neutralise la configuration MCP locale avec `mcp_servers={}` ;
- échoue fermé si le flux JSON signale le démarrage d'un item outil ;
- s'exécute dans un répertoire temporaire vide et reçoit uniquement un environnement filtré ;
- reçoit la question et les passages ETPOS par stdin.

### Transport `app-server`

Le processus App Server peut rester chaud au niveau du worker FastAPI, mais chaque question démarre un thread `ephemeral` dans un répertoire temporaire distinct. L'application impose :

- sandbox `read-only` et politique d'approbation `never` ;
- recherche Web désactivée ;
- fonctions shell, navigateur, computer use, plugins, skills et autres capacités non textuelles désactivées au démarrage ;
- configuration MCP locale neutralisée par `mcp_servers={}` et fonctionnalités MCP/elicitation associées désactivées ;
- les champs App Server expérimentaux `dynamicTools`, `environments`, `runtimeWorkspaceRoots` et `selectedCapabilityRoots` ne sont pas envoyés sans capability `experimentalApi` dédiée ;
- instructions de base et développeur limitées à un moteur de réponse textuel utilisant uniquement le texte fourni ;
- refus explicite des demandes d'approbation de commandes ou changements de fichiers ;
- échec fermé si Codex démarre un type d'item outil inattendu ;
- environnement filtré identique au transport `exec` ;
- en production, un `ETPOS_CODEX_APP_HOME` dédié et authentifié directement est obligatoire.

En développement, lorsqu'aucun `ETPOS_CODEX_APP_HOME` n'est défini, l'application crée un CODEX_HOME temporaire contenant uniquement une copie protégée de `auth.json` ; la configuration utilisateur, les règles et les autres fichiers du CODEX_HOME source ne sont pas copiés. En production, cette copie implicite est refusée.

Dans les deux transports, le prompt ne reçoit jamais la base utilisateurs, les cookies, les secrets de l'application ou le chemin du projet. Il reçoit seulement la question, l'historique conversationnel sélectionné et les passages ETPOS récupérés par le backend. Le backend reste propriétaire du retrieval et des citations.

### Risque résiduel

Codex CLI reste un agent de développement et le sandbox `read-only` n'est pas équivalent à une preuve formelle d'absence de toute capacité locale. Les garde-fous App Server réduisent davantage la surface exposée et échouent fermés sur les événements outils connus/inattendus, mais ils dépendent aussi des garanties du runtime Codex. Avant une ouverture à des utilisateurs non fiables ou à grande échelle, une nouvelle revue d'isolation est obligatoire ; une isolation de processus/conteneur ou un mécanisme d'inférence plus strict pourra alors être nécessaire.

Les identifiants ChatGPT/Codex sont sensibles. `CODEX_HOME`, `ETPOS_CODEX_APP_HOME` et notamment `auth.json` doivent être protégés comme un mot de passe, appartenir uniquement au compte de service et ne jamais être copiés dans le dépôt.

## En-têtes applicatifs

L'application ajoute notamment : CSP restrictive, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `Permissions-Policy` restrictive et `frame-ancestors 'none'`. `same-origin` est volontaire : le contrôle anti-CSRF du login peut utiliser `Referer` comme signal de même origine lorsque `Origin` n'est pas envoyé, tout en évitant d'envoyer le référent vers un site externe. La dictée autorise explicitement `microphone=(self)` uniquement pour l'origine ETPOS Assistant ; caméra et géolocalisation restent refusées. La CSP conserve `connect-src 'self'` sans joker ; toute exception WebSocket future doit être limitée à l'origine exacte réellement validée dans les navigateurs ciblés.

## Dictée HTTP et WebSocket

Le chemin POST classique reste la référence. Il exige session + CSRF, borne type, taille et durée, utilise un fichier temporaire supprimé après transcription et ne persiste jamais l'audio.

Le chemin incrémental `/api/transcribe/stream` reste désactivé par défaut. Lorsqu'il est activé :

- l'origine WebSocket doit correspondre exactement à l'origine publique attendue ;
- une session valide est requise avant admission ;
- le premier message applicatif `init` doit contenir le CSRF attendu ;
- le flux est PCM mono 16 kHz s16le ; séquence et position absolue doivent être contiguës ;
- chaque message est borné à 16 Kio et la durée totale reprend la limite Whisper globale ;
- le frontend applique un plafond de backpressure correspondant à quatre messages maximum sur `WebSocket.bufferedAmount` ;
- Uvicorn est préparé avec `websockets-sansio`, qui suspend la lecture réseau jusqu'à consommation du message ASGI courant ; l'ancienne option `ws_max_queue` n'est pas utilisée comme garantie de sécurité ;
- avec un worker, l'admission commune POST/WebSocket autorise une seule dictée lourde à la fois. Un POST concurrent reçoit HTTP 409 ; un WebSocket concurrent reçoit `busy` puis une fermeture 1013 ;
- les erreurs de protocole utilisent une fermeture de politique 1008 ; un timeout de finalisation utilise 1013 et une indisponibilité interne de transcription utilise 1011.

Le PCM du WebSocket reste uniquement en mémoire pendant la session. Les logs de dictée contiennent des tailles, positions, durées, compteurs, coûts et identifiants techniques aléatoires ; ils ne doivent jamais journaliser l'audio, le texte transcrit, le CSRF, le cookie de session ou les credentials Codex. Les aperçus ne sont jamais considérés comme du texte final.

`ETPOS_WHISPER_STREAM_PAUSE_FINALIZATION_ENABLED` reste lui aussi désactivé tant que la segmentation par pauses n'a pas démontré une qualité acceptable et un gain mesuré sur la machine cible. En cas d'incohérence de couverture ou d'échec d'une portion, la finalisation globale reste le fallback.

## Production

En production :

- définir `ETPOS_COOKIE_SECURE=true` ;
- placer le service derrière HTTPS/Nginx ;
- utiliser un utilisateur Unix dédié ;
- utiliser un `CODEX_HOME` dédié hors du dépôt ;
- utiliser `ETPOS_CODEX_TRANSPORT=app-server` comme transport de production tant que les validations réelles restent satisfaisantes ;
- utiliser un `ETPOS_CODEX_APP_HOME` dédié et authentifié directement sous l'utilisateur Unix du service ;
- conserver `ETPOS_CODEX_TRANSPORT=exec` comme rollback immédiat ;
- ne pas installer une politique Codex globale uniquement pour ETPOS sur un VPS partagé : elle pourrait modifier Hermes ou d'autres usages Codex ;
- faire `codex login --device-auth` sous l'utilisateur dédié au service ;
- ne jamais définir `OPENAI_API_KEY` pour cette application ;
- limiter les permissions Unix du répertoire Codex et des bases SQLite.
