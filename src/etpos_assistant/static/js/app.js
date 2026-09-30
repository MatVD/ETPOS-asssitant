(() => {
  const form = document.getElementById("chat-form");
  const input = document.getElementById("message-input");
  const send = document.getElementById("send-button");
  const stop = document.getElementById("stop-button");
  const dictation = document.getElementById("dictation-button");
  const composerStatus = document.getElementById("composer-status");
  const dictationSignal = document.getElementById("dictation-signal");
  if (dictationSignal && dictationSignal.children.length === 0) {
    const fragment = document.createDocumentFragment();
    for (let index = 0; index < 120; index += 1) {
      const bar = document.createElement("span");
      bar.className = "dictation-signal-bar";
      fragment.appendChild(bar);
    }
    dictationSignal.appendChild(fragment);
  }
  const dictationSignalBars = dictationSignal
    ? Array.from(dictationSignal.querySelectorAll(".dictation-signal-bar"))
    : [];
  const messages = document.getElementById("messages");
  const main = document.querySelector(".chat-main");
  const shell = document.getElementById("app-shell");
  const sidebar = document.getElementById("conversation-sidebar");
  const sidebarToggle = document.getElementById("sidebar-toggle");
  const sidebarClose = document.getElementById("sidebar-close");
  const sidebarBackdrop = document.getElementById("sidebar-backdrop");
  const conversationList = document.querySelector(".conversation-list");
  const historySearch = document.getElementById("history-search");
  const announcer = document.getElementById("chat-announcer");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const mobileSidebar = window.matchMedia("(max-width: 800px)");
  let conversationLinks = conversationList
    ? Array.from(conversationList.querySelectorAll(".conversation-link"))
    : [];
  let activeGeneration = null;
  let activeRecorder = null;
  let activeVoiceStream = null;
  let dictationBusy = false;
  let dictationState = "idle";
  let dictationSnapshot = null;
  let dictationStopTimer = null;
  let dictationLevels = Array(Math.max(1, dictationSignalBars.length)).fill(0);
  let sidebarPreviousFocus = null;
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const voiceStreamingEnabled = main?.dataset.voiceStreamingEnabled === "true";

  const announce = (message) => {
    if (!announcer) return;
    announcer.textContent = "";
    requestAnimationFrame(() => {
      announcer.textContent = message;
    });
  };

  const normalizeHistoryText = (value) => (
    String(value || "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLocaleLowerCase("fr-FR")
  );

  const conversationGroupLabel = (isoDate) => {
    const date = new Date(isoDate);
    if (Number.isNaN(date.getTime())) return "Plus ancien";

    const now = new Date();
    const localDay = (value) => Date.UTC(
      value.getFullYear(),
      value.getMonth(),
      value.getDate(),
    );
    const days = Math.round((localDay(now) - localDay(date)) / 86400000);

    if (days <= 0) return "Aujourd’hui";
    if (days === 1) return "Hier";
    if (days <= 7) return "7 derniers jours";
    if (days <= 30) return "30 derniers jours";
    return "Plus ancien";
  };

  const rebuildConversationHistory = () => {
    if (!conversationList) return;

    const links = conversationLinks;
    const query = normalizeHistoryText(historySearch?.value.trim());
    const matching = links.filter((link) => (
      !query || normalizeHistoryText(link.textContent).includes(query)
    ));

    conversationList.replaceChildren();

    if (links.length === 0) {
      const empty = document.createElement("p");
      empty.className = "sidebar-empty";
      empty.textContent = "Aucune conversation.";
      conversationList.appendChild(empty);
      return;
    }

    if (matching.length === 0) {
      const empty = document.createElement("p");
      empty.className = "sidebar-empty";
      empty.textContent = "Aucune conversation trouvée.";
      conversationList.appendChild(empty);
      return;
    }

    const groups = new Map();
    matching.forEach((link) => {
      const updatedAt = link.dataset.updatedAt || "";
      const label = conversationGroupLabel(updatedAt);
      if (!groups.has(label)) groups.set(label, []);
      groups.get(label).push(link);

      const date = new Date(updatedAt);
      if (!Number.isNaN(date.getTime())) {
        const formatted = new Intl.DateTimeFormat("fr-FR", {
          dateStyle: "medium",
          timeStyle: "short",
        }).format(date);
        link.title = `${link.textContent.trim()} — ${formatted}`;
      }
    });

    groups.forEach((groupLinks, label) => {
      const section = document.createElement("section");
      section.className = "conversation-group";
      const heading = document.createElement("h2");
      heading.className = "conversation-group-title";
      heading.textContent = label;
      section.appendChild(heading);
      groupLinks.forEach((link) => section.appendChild(link));
      conversationList.appendChild(section);
    });
  };

  historySearch?.addEventListener("input", rebuildConversationHistory);
  rebuildConversationHistory();

  const setSidebarOpen = (open) => {
    if (!shell || !sidebarToggle) return;
    const wasOpen = shell.classList.contains("sidebar-open");
    shell.classList.toggle("sidebar-open", open);
    document.body.classList.toggle("menu-open", open);
    sidebarToggle.setAttribute("aria-expanded", open ? "true" : "false");

    if (open && !wasOpen && mobileSidebar.matches) {
      sidebarPreviousFocus = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : sidebarToggle;
      main?.setAttribute("inert", "");
      requestAnimationFrame(() => {
        (historySearch || sidebarClose)?.focus({ preventScroll: true });
      });
    } else if (!open && wasOpen) {
      main?.removeAttribute("inert");
      const target = sidebarPreviousFocus?.isConnected
        ? sidebarPreviousFocus
        : sidebarToggle;
      sidebarPreviousFocus = null;
      requestAnimationFrame(() => target?.focus({ preventScroll: true }));
    }
  };

  sidebarToggle?.addEventListener("click", () => {
    setSidebarOpen(!shell?.classList.contains("sidebar-open"));
  });
  sidebarClose?.addEventListener("click", () => setSidebarOpen(false));
  sidebarBackdrop?.addEventListener("click", () => setSidebarOpen(false));
  conversationList?.addEventListener("click", (event) => {
    if (event.target.closest(".conversation-link")) setSidebarOpen(false);
  });
  mobileSidebar.addEventListener("change", (event) => {
    if (!event.matches && shell?.classList.contains("sidebar-open")) {
      setSidebarOpen(false);
    }
  });
  document.addEventListener("keydown", (event) => {
    if (!shell?.classList.contains("sidebar-open")) return;

    if (event.key === "Escape") {
      event.preventDefault();
      setSidebarOpen(false);
      return;
    }

    if (event.key === "Tab" && sidebar) {
      const focusable = Array.from(
        sidebar.querySelectorAll(
          'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (focusable.length === 0) return;

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (!sidebar.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      }
    }
  });

  document.querySelectorAll(".suggestion").forEach((button) => {
    button.addEventListener("click", () => {
      if (!input) return;
      input.value = button.textContent || "";
      resize();
      input.focus();
    });
  });

  const back = document.getElementById("back-link");
  if (back) {
    back.addEventListener("click", (event) => {
      if (history.length > 1) {
        event.preventDefault();
        history.back();
      }
    });
  }

  if (!form || !input || !send || !stop || !messages || !main) return;

  const resize = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 190)}px`;
  };
  input.addEventListener("input", resize);
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    if (!send.disabled) form.requestSubmit();
  });

  const setComposerStatus = (text = "", isError = false) => {
    if (!composerStatus) return;
    composerStatus.textContent = text;
    composerStatus.classList.toggle("error", Boolean(isError));
  };

  const resetDictationSignal = () => {
    dictationLevels = Array(Math.max(1, dictationSignalBars.length)).fill(0);
    dictationSignalBars.forEach((bar) => {
      bar.style.transform = "scaleY(.1)";
    });
  };

  const setDictationWaveVisible = (visible) => {
    if (dictationSignal) dictationSignal.hidden = !visible;
    form.classList.toggle("dictation-wave-active", Boolean(visible));
    if (!visible) resetDictationSignal();
  };

  const updateDictationSignal = (level) => {
    if (!dictationSignal || dictationSignal.hidden || dictationSignalBars.length === 0) return;
    const numeric = Math.max(0, Math.min(1, Number(level) || 0));
    const visual = Math.min(1, Math.sqrt(numeric) * 2.2);
    dictationLevels.shift();
    dictationLevels.push(visual);
    dictationSignalBars.forEach((bar, index) => {
      bar.style.transform = "scaleY(" + Math.max(.1, dictationLevels[index] || 0).toFixed(3) + ")";
    });
  };

  const supportedRecorderMimeType = () => {
    if (typeof MediaRecorder === "undefined") return "";
    const candidates = [
      "audio/webm;codecs=opus",
      "audio/mp4",
      "audio/ogg;codecs=opus",
      "audio/webm",
      "audio/ogg",
    ];
    return candidates.find((type) => MediaRecorder.isTypeSupported?.(type)) || "";
  };

  const setRetryDisabled = (disabled) => {
    document.querySelectorAll(".retry-button").forEach((button) => {
      button.disabled = Boolean(disabled);
    });
  };

  const setDictationState = (state) => {
    dictationState = state;
    const recording = state === "recording";
    const starting = state === "starting";
    const finishing = state === "finishing";
    const transcribing = state === "transcribing";
    const active = state !== "idle";
    if (dictation) {
      dictation.classList.toggle("recording", recording);
      dictation.classList.toggle("transcribing", starting || finishing || transcribing);
      dictation.setAttribute("aria-pressed", recording ? "true" : "false");
      dictation.setAttribute(
        "aria-label",
        recording
          ? "Arrêter la dictée"
          : starting
            ? "Démarrage de la dictée"
            : finishing || transcribing
              ? "Transcription en cours"
              : "Dicter une question",
      );
      dictation.disabled = starting || finishing || transcribing || Boolean(activeGeneration);
    }
    send.disabled = active || Boolean(activeGeneration);
    setRetryDisabled(active || Boolean(activeGeneration));
  };

  const composeTranscriptionValue = (baseValue, text, selection) => {
    const transcript = String(text || "").trim();
    if (!transcript) return null;
    const start = selection?.start ?? baseValue.length;
    const end = selection?.end ?? start;
    const before = baseValue.slice(0, start);
    const after = baseValue.slice(end);
    const needsSpaceBefore = before && !/\s$/.test(before);
    const needsSpaceAfter = after && !/^\s/.test(after);
    const inserted = (needsSpaceBefore ? " " : "") + transcript + (needsSpaceAfter ? " " : "");
    const value = (before + inserted + after).slice(0, input.maxLength || 4000);
    return {
      value,
      caret: Math.min(before.length + inserted.length, value.length),
    };
  };

  const applyComposedTranscription = (composed) => {
    if (!composed) return;
    input.value = composed.value;
    input.setSelectionRange(composed.caret, composed.caret);
    resize();
    input.focus({ preventScroll: true });
    input.dispatchEvent(new Event("input", { bubbles: true }));
  };

  const insertTranscription = (text, selection = null) => {
    const start = selection?.start ?? input.selectionStart ?? input.value.length;
    const end = selection?.end ?? input.selectionEnd ?? start;
    applyComposedTranscription(
      composeTranscriptionValue(input.value, text, { start, end }),
    );
  };

  const captureDictationSnapshot = () => ({
    value: input.value,
    start: input.selectionStart ?? input.value.length,
    end: input.selectionEnd ?? input.selectionStart ?? input.value.length,
  });

  const stopRecorderTracks = (recorderState) => {
    recorderState?.stream?.getTracks?.().forEach((track) => track.stop());
  };

  async function transcribeRecording(blob) {
    if (!blob || blob.size === 0) {
      setDictationState("idle");
      setComposerStatus("Aucun son n’a été enregistré.", true);
      return;
    }

    dictationBusy = true;
    setDictationState("transcribing");
    setComposerStatus("Transcription en cours…");
    try {
      const response = await fetch("/api/transcribe", {
        method: "POST",
        headers: {
          "Content-Type": blob.type || "audio/webm",
          "X-CSRF-Token": csrf,
        },
        body: blob,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || "La transcription n’a pas abouti.");
      }
      insertTranscription(payload.text || "");
      setComposerStatus("");
      announce("Transcription ajoutée au champ de question.");
    } catch (error) {
      const message = error?.message || "La transcription n’a pas abouti.";
      setComposerStatus(message, true);
      announce(message);
    } finally {
      dictationBusy = false;
      dictationSnapshot = null;
      setDictationState("idle");
    }
  }

  async function startClassicDictation() {
    if (!dictation || dictationBusy || activeGeneration || dictationState !== "idle") return;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setComposerStatus("La dictée n’est pas disponible dans ce navigateur ou hors HTTPS.", true);
      return;
    }

    dictationSnapshot = captureDictationSnapshot();
    setDictationState("starting");
    setComposerStatus("");
    let stream = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      });
      const mimeType = supportedRecorderMimeType();
      const recorder = mimeType
        ? new MediaRecorder(stream, { mimeType, audioBitsPerSecond: 64000 })
        : new MediaRecorder(stream);
      const state = { recorder, stream, chunks: [] };
      activeRecorder = state;

      recorder.addEventListener("dataavailable", (event) => {
        if (event.data?.size) state.chunks.push(event.data);
      });
      recorder.addEventListener("stop", async () => {
        if (dictationStopTimer) {
          clearTimeout(dictationStopTimer);
          dictationStopTimer = null;
        }
        stopRecorderTracks(state);
        if (activeRecorder === state) activeRecorder = null;
        const actualType = recorder.mimeType || mimeType || state.chunks[0]?.type || "audio/webm";
        const blob = new Blob(state.chunks, { type: actualType });
        await transcribeRecording(blob);
      }, { once: true });
      recorder.addEventListener("error", () => {
        if (dictationStopTimer) {
          clearTimeout(dictationStopTimer);
          dictationStopTimer = null;
        }
        stopRecorderTracks(state);
        if (activeRecorder === state) activeRecorder = null;
        setDictationState("idle");
        setComposerStatus("L’enregistrement audio a échoué.", true);
      }, { once: true });

      recorder.start();
      setDictationState("recording");
      setComposerStatus("Écoute en cours…");
      announce("Dictée démarrée.");
      dictationStopTimer = setTimeout(() => {
        if (recorder.state === "recording") recorder.stop();
      }, 59000);
    } catch (error) {
      stream?.getTracks?.().forEach((track) => track.stop());
      if (activeRecorder?.stream === stream) activeRecorder = null;
      let message = "Impossible d’accéder au microphone.";
      if (error?.name === "NotAllowedError" || error?.name === "SecurityError") {
        message = "Accès au microphone refusé. Autorisez-le dans le navigateur puis réessayez.";
      } else if (error?.name === "NotFoundError") {
        message = "Aucun microphone n’a été détecté.";
      }
      setDictationState("idle");
      dictationSnapshot = null;
      setComposerStatus(message, true);
      announce(message);
    }
  }

  const handleStreamingFinal = (text, controller) => {
    if (activeVoiceStream !== controller) return;
    activeVoiceStream = null;
    const snapshot = dictationSnapshot;
    dictationSnapshot = null;
    setDictationWaveVisible(false);
    setDictationState("idle");

    const decision = window.finalInsertionDecision?.(snapshot, input.value, text)
      || {
        mode: snapshot && input.value === snapshot.value ? "insert" : "review",
        text: String(text || "").trim(),
        selection: snapshot ? { start: snapshot.start, end: snapshot.end } : null,
      };
    if (decision.mode === "insert") {
      insertTranscription(decision.text, decision.selection);
      setComposerStatus("");
      announce("Transcription ajoutée au champ de question.");
      return;
    }

    setComposerStatus(
      decision.mode === "review" && decision.text
        ? "Le champ a été modifié pendant la dictée. La transcription finale n’a pas été appliquée."
        : "",
    );
  };

  async function startStreamingDictation() {
    if (!dictation || activeGeneration || dictationState !== "idle") return;
    dictationSnapshot = captureDictationSnapshot();
    setComposerStatus("");

    const controller = new window.ETPOSVoiceStream({
      csrfToken: csrf,
      onState(state) {
        if (activeVoiceStream !== controller) return;
        setDictationState(state);
        setDictationWaveVisible(["starting", "recording", "finishing"].includes(state));
        if (state === "starting") setComposerStatus("Connexion de la dictée…");
        if (state === "recording") {
          setComposerStatus("Écoute en cours…");
          announce("Dictée démarrée.");
        }
        if (state === "finishing") setComposerStatus("Finalisation de la transcription…");
      },
      onLevel(level) {
        if (activeVoiceStream !== controller || dictationState !== "recording") return;
        updateDictationSignal(level);
      },
      onFinal(text) {
        handleStreamingFinal(text, controller);
      },
      onError(error) {
        if (activeVoiceStream !== controller) return;
        activeVoiceStream = null;
        dictationSnapshot = null;
        setDictationWaveVisible(false);
        setDictationState("idle");
        const message = error?.message || "La dictée n’a pas abouti.";
        setComposerStatus(message, true);
        announce(message);
      },
    });
    activeVoiceStream = controller;
    try {
      await controller.start();
    } catch {
      // Le contrôleur a déjà rendu l'erreur terminale via onError.
    }
  }

  async function startDictation() {
    const streamingSupported = voiceStreamingEnabled
      && Boolean(window.ETPOSVoiceStream?.supported?.(window));
    if (streamingSupported) {
      await startStreamingDictation();
      return;
    }
    await startClassicDictation();
  }

  function stopDictation() {
    if (activeVoiceStream && dictationState === "recording") {
      void activeVoiceStream.stop();
      return;
    }

    const recorder = activeRecorder?.recorder;
    if (recorder?.state === "recording") {
      recorder.stop();
      setComposerStatus("Préparation de la transcription…");
    }
  }

  if (dictation) {
    const classicSupported = window.isSecureContext
      && Boolean(navigator.mediaDevices?.getUserMedia)
      && typeof MediaRecorder !== "undefined";
    const streamingSupported = voiceStreamingEnabled
      && Boolean(window.ETPOSVoiceStream?.supported?.(window));
    const supported = streamingSupported || classicSupported;
    dictation.hidden = !supported;
    if (supported) {
      dictation.addEventListener("click", () => {
        if (
          (activeVoiceStream && dictationState === "recording")
          || activeRecorder?.recorder?.state === "recording"
        ) {
          stopDictation();
        } else {
          void startDictation();
        }
      });
    }
  }

  window.addEventListener("pagehide", () => {
    if (dictationStopTimer) clearTimeout(dictationStopTimer);
    stopRecorderTracks(activeRecorder);
    if (activeVoiceStream) void activeVoiceStream.cancel();
  });

  const isNearBottom = (threshold = 180) => (
    window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - threshold
  );

  const scrollToBottom = (behavior = "auto") => {
    window.scrollTo({
      top: document.documentElement.scrollHeight,
      behavior: reduceMotion.matches ? "auto" : behavior,
    });
  };

  const streamingPreview = (markdown) => (
    markdown
      .replace(/^\s*```[^\n]*$/gm, "")
      .replace(/`([^`\n]+)`/g, "$1")
      .replace(/\*\*/g, "")
      .replace(/__/g, "")
      .replace(/^#{1,6}\s+/gm, "")
      .replace(/^>\s?/gm, "")
      .replace(/^\s*[-*+]\s+/gm, "• ")
  );

  function addMessage(role, text) {
    document.getElementById("empty-state")?.remove();
    const article = document.createElement("article");
    article.className = `message ${role}`;
    const label = document.createElement("div");
    label.className = "message-label";
    label.textContent = role === "user" ? "Vous" : "ETPOS Assistant";
    const progress = document.createElement("div");
    progress.className = "message-progress";
    progress.hidden = true;
    progress.setAttribute("role", "status");
    const body = document.createElement("div");
    body.className = "message-body";
    const p = document.createElement("p");
    p.textContent = text;
    body.appendChild(p);
    article.append(label, progress, body);
    messages.appendChild(article);
    return {
      article,
      body,
      paragraph: p,
      progress,
      startedAt: performance.now(),
      firstTextAt: null,
    };
  }

  function setAssistantStatus(assistant, text, stage = "") {
    assistant.progress.textContent = text;
    assistant.progress.dataset.stage = stage;
    assistant.progress.hidden = !text;
    assistant.body.hidden = Boolean(text);
  }

  function showAssistantContent(assistant) {
    assistant.progress.hidden = true;
    assistant.progress.textContent = "";
    assistant.body.hidden = false;
  }

  function renderRetryState(article, message) {
    article.classList.add("retryable");
    article.classList.remove("error");
    article.removeAttribute("aria-busy");
    article.querySelector(".answer-status")?.remove();
    article.querySelector(".citations")?.remove();
    article.querySelector(".message-progress")?.remove();

    let body = article.querySelector(".message-body");
    if (!body) {
      body = document.createElement("div");
      body.className = "message-body";
      article.appendChild(body);
    }
    body.classList.remove("streaming");
    body.hidden = false;
    body.replaceChildren();
    const paragraph = document.createElement("p");
    paragraph.textContent = message;
    body.appendChild(paragraph);

    article.querySelector(".message-actions")?.remove();
    let retry = null;
    if (main.dataset.conversationId) {
      const actions = document.createElement("div");
      actions.className = "message-actions";
      retry = document.createElement("button");
      retry.type = "button";
      retry.className = "retry-button";
      retry.textContent = "Réessayer";
      actions.appendChild(retry);
      article.appendChild(actions);
    }
    return retry;
  }

  function setGenerating(active) {
    const stopHadFocus = document.activeElement === stop;
    send.disabled = active || dictationState !== "idle" || dictationBusy || Boolean(activeRecorder);
    send.hidden = active;
    stop.hidden = !active;
    stop.disabled = !active;
    if (dictation) dictation.disabled = active || dictationBusy || dictationState !== "idle";
    setRetryDisabled(active || dictationState !== "idle");
    if (active) {
      form.setAttribute("aria-busy", "true");
    } else {
      form.removeAttribute("aria-busy");
      if (stopHadFocus) {
        requestAnimationFrame(() => input.focus({ preventScroll: true }));
      }
    }
  }

  function renderAnswerStatus(article, status) {
    article.querySelector(".answer-status")?.remove();
    if (!["partial", "none"].includes(status)) return;

    const badge = document.createElement("div");
    badge.className = `answer-status ${status}`;
    badge.textContent = status === "partial"
      ? "Documentation partielle"
      : "Documentation insuffisante";
    article.querySelector(".message-label")?.after(badge);
  }

  function renderCitations(article, citations) {
    article.querySelector(".citations")?.remove();
    if (!Array.isArray(citations) || citations.length === 0) return;

    const details = document.createElement("details");
    details.className = "citations";
    const summary = document.createElement("summary");
    summary.append("Sources ");
    const count = document.createElement("span");
    count.className = "citation-count";
    count.textContent = String(citations.length);
    summary.appendChild(count);

    const list = document.createElement("div");
    list.className = "citation-list";
    citations.forEach((citation) => {
      const link = document.createElement("a");
      link.className = "citation-card";
      link.dataset.sourceId = citation.source_id;
      link.href = citation.internal_url;
      const strong = document.createElement("strong");
      strong.textContent = `${citation.source_id} — ${citation.heading_path || citation.title}`;
      const meta = document.createElement("span");
      meta.textContent = `${citation.document_name}${citation.version ? ` · ${citation.version}` : ""}`;
      link.append(strong, meta);
      list.appendChild(link);
    });

    details.append(summary, list);
    article.appendChild(details);
  }

  function linkCitationMarkers(article) {
    const body = article.querySelector(".message-body");
    if (!body) return;

    const sources = new Map(
      Array.from(article.querySelectorAll(".citation-card[data-source-id]"))
        .map((link) => [link.dataset.sourceId, link.getAttribute("href")]),
    );
    if (sources.size === 0) return;

    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const value = node.nodeValue || "";
        const parent = node.parentElement;
        if (!/\[S\d+\]/.test(value) || parent?.closest("a, code, pre")) {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);

    nodes.forEach((node) => {
      const text = node.nodeValue || "";
      const fragment = document.createDocumentFragment();
      const pattern = /\[(S\d+)\]/g;
      let cursor = 0;
      let match;

      while ((match = pattern.exec(text)) !== null) {
        if (match.index > cursor) {
          fragment.append(text.slice(cursor, match.index));
        }
        const href = sources.get(match[1]);
        if (href) {
          const link = document.createElement("a");
          link.className = "citation-marker";
          link.href = href;
          link.textContent = match[0];
          link.setAttribute("aria-label", `Voir la source ${match[1]}`);
          fragment.appendChild(link);
        } else {
          fragment.append(match[0]);
        }
        cursor = pattern.lastIndex;
      }

      if (cursor > 0) {
        if (cursor < text.length) fragment.append(text.slice(cursor));
        node.replaceWith(fragment);
      }
    });
  }

  function decorateMenuPaths(article) {
    article.querySelectorAll(".message-body p").forEach((paragraph) => {
      const first = paragraph.firstElementChild;
      if (
        first?.tagName === "STRONG"
        && /^chemin\s*:?$/i.test((first.textContent || "").trim())
      ) {
        paragraph.classList.add("menu-path");
      }
    });
  }

  function enhanceAssistantMessage(article) {
    linkCitationMarkers(article);
    decorateMenuPaths(article);
  }

  function syncConversationList(conversationId, title = "") {
    if (!conversationList) return;
    const id = String(conversationId);
    conversationLinks.forEach((link) => {
      link.classList.remove("active");
      link.removeAttribute("aria-current");
    });
    let link = conversationLinks.find((item) => item.dataset.conversationId === id);
    const isNew = !link;

    if (!link) {
      link = document.createElement("a");
      link.className = "conversation-link";
      link.dataset.conversationId = id;
      link.href = `/c/${id}`;
      link.textContent = title || "Nouvelle conversation";
    } else if (title) {
      link.textContent = title;
    }

    link.dataset.updatedAt = new Date().toISOString();
    link.classList.add("active");
    link.setAttribute("aria-current", "page");
    conversationLinks = [
      link,
      ...conversationLinks.filter((item) => item !== link),
    ];
    if (isNew && historySearch) historySearch.value = "";
    rebuildConversationHistory();
  }

  async function consumeSSE(response, assistant) {
    if (!response.body) throw new Error("Flux indisponible");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let accumulated = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
        const line = frame.split("\n").find((item) => item.startsWith("data:"));
        if (!line) continue;
        const event = JSON.parse(line.slice(5).trim());
        const followResponse = isNearBottom();

        if (event.type === "meta") {
          main.dataset.conversationId = String(event.conversation_id);
          syncConversationList(event.conversation_id, event.conversation_title || "");
          if (location.pathname === "/") history.replaceState({}, "", `/c/${event.conversation_id}`);
        } else if (event.type === "status") {
          setAssistantStatus(assistant, event.text || "", event.stage || "");
        } else if (event.type === "delta") {
          if (assistant.firstTextAt === null) {
            assistant.firstTextAt = performance.now();
            assistant.article.dataset.firstTextMs = String(
              Math.round(assistant.firstTextAt - assistant.startedAt),
            );
          }
          showAssistantContent(assistant);
          assistant.body.classList.add("streaming");
          accumulated += event.text || "";
          assistant.paragraph.textContent = streamingPreview(accumulated);
        } else if (event.type === "done") {
          showAssistantContent(assistant);
          assistant.body.classList.remove("streaming");
          assistant.body.innerHTML = event.html || "";
          assistant.article.dataset.completeMs = String(
            Math.round(performance.now() - assistant.startedAt),
          );
          assistant.article.classList.remove("retryable", "error");
          assistant.article.querySelector(".message-actions")?.remove();
          assistant.article.removeAttribute("aria-busy");
          renderAnswerStatus(assistant.article, event.answer_status || "");
          renderCitations(assistant.article, event.citations || []);
          enhanceAssistantMessage(assistant.article);
          announce("Réponse terminée.");
        } else if (event.type === "error") {
          const message = event.message || "La réponse n’a pas pu être générée.";
          renderRetryState(assistant.article, message);
          announce(message);
        }

        if (followResponse && ["status", "delta", "done", "error"].includes(event.type)) {
          scrollToBottom();
        }
      }
    }
  }

  document.querySelectorAll(".message.assistant").forEach(enhanceAssistantMessage);

  const navigationEntry = performance.getEntriesByType("navigation")[0];
  if (
    main.dataset.conversationId
    && messages.querySelector(".message")
    && navigationEntry?.type !== "back_forward"
  ) {
    requestAnimationFrame(() => scrollToBottom());
  }

  async function runGeneration(endpoint, payload, assistant) {
    const generation = {
      controller: new AbortController(),
      assistant,
      stoppedByUser: false,
    };
    activeGeneration = generation;
    assistant.article.setAttribute("aria-busy", "true");
    setAssistantStatus(assistant, "Je vérifie la documentation ETPOS…", "requesting");
    setGenerating(true);

    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify(payload),
        signal: generation.controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await consumeSSE(response, assistant);
    } catch (error) {
      if (error?.name === "AbortError" && generation.stoppedByUser) {
        const retry = renderRetryState(assistant.article, "Réponse interrompue.");
        announce("Réponse interrompue. Vous pouvez réessayer.");
        requestAnimationFrame(() => retry?.focus({ preventScroll: true }));
      } else if (error?.name !== "AbortError") {
        const message = "La requête n’a pas abouti. Vérifiez votre connexion puis réessayez.";
        renderRetryState(assistant.article, message);
        announce(message);
        console.error(error);
      }
    } finally {
      if (activeGeneration === generation) {
        activeGeneration = null;
        setGenerating(false);
      }
    }
  }

  stop.addEventListener("click", () => {
    if (!activeGeneration) return;
    activeGeneration.stoppedByUser = true;
    stop.disabled = true;
    activeGeneration.controller.abort();
  });

  messages.addEventListener("click", async (event) => {
    const retry = event.target.closest(".retry-button");
    if (!retry || activeGeneration || dictationState !== "idle") return;

    const conversationId = Number(main.dataset.conversationId);
    if (!conversationId) return;

    const previous = retry.closest(".message.assistant");
    previous?.remove();

    const assistant = addMessage("assistant", "");
    input.focus({ preventScroll: true });
    scrollToBottom("smooth");
    await runGeneration(
      "/api/chat/retry",
      { conversation_id: conversationId },
      assistant,
    );
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || activeGeneration || dictationState !== "idle" || dictationBusy || activeRecorder) return;

    addMessage("user", text);
    const assistant = addMessage("assistant", "");
    input.value = "";
    resize();
    input.focus({ preventScroll: true });
    scrollToBottom("smooth");

    const conversationId = main.dataset.conversationId
      ? Number(main.dataset.conversationId)
      : null;
    await runGeneration(
      "/api/chat",
      { message: text, conversation_id: conversationId },
      assistant,
    );
  });
})();
