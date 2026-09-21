(() => {
  const form = document.getElementById("chat-form");
  const input = document.getElementById("message-input");
  const send = document.getElementById("send-button");
  const messages = document.getElementById("messages");
  const main = document.querySelector(".chat-main");
  const shell = document.getElementById("app-shell");
  const sidebarToggle = document.getElementById("sidebar-toggle");
  const sidebarClose = document.getElementById("sidebar-close");
  const sidebarBackdrop = document.getElementById("sidebar-backdrop");
  const conversationList = document.querySelector(".conversation-list");
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  const setSidebarOpen = (open) => {
    if (!shell || !sidebarToggle) return;
    shell.classList.toggle("sidebar-open", open);
    document.body.classList.toggle("menu-open", open);
    sidebarToggle.setAttribute("aria-expanded", open ? "true" : "false");
  };

  sidebarToggle?.addEventListener("click", () => {
    setSidebarOpen(!shell?.classList.contains("sidebar-open"));
  });
  sidebarClose?.addEventListener("click", () => setSidebarOpen(false));
  sidebarBackdrop?.addEventListener("click", () => setSidebarOpen(false));
  conversationList?.addEventListener("click", (event) => {
    if (event.target.closest(".conversation-link")) setSidebarOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && shell?.classList.contains("sidebar-open")) {
      setSidebarOpen(false);
      sidebarToggle?.focus();
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

  if (!form || !input || !send || !messages || !main) return;

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

  const isNearBottom = (threshold = 180) => (
    window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - threshold
  );

  const scrollToBottom = (behavior = "auto") => {
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior });
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

  function renderCitations(article, citations) {
    if (!Array.isArray(citations) || citations.length === 0) return;
    const wrap = document.createElement("div");
    wrap.className = "citations";
    citations.forEach((citation) => {
      const link = document.createElement("a");
      link.className = "citation-card";
      link.href = citation.internal_url;
      const strong = document.createElement("strong");
      strong.textContent = `${citation.source_id} — ${citation.heading_path || citation.title}`;
      const meta = document.createElement("span");
      meta.textContent = `${citation.document_name}${citation.version ? ` · ${citation.version}` : ""}`;
      link.append(strong, meta);
      wrap.appendChild(link);
    });
    article.appendChild(wrap);
  }

  function syncConversationList(conversationId, title = "") {
    if (!conversationList) return;
    const id = String(conversationId);
    const links = Array.from(conversationList.querySelectorAll(".conversation-link"));
    links.forEach((link) => link.classList.remove("active"));
    let link = links.find((item) => item.dataset.conversationId === id);

    if (!link) {
      link = document.createElement("a");
      link.className = "conversation-link";
      link.dataset.conversationId = id;
      link.href = `/c/${id}`;
      link.textContent = title || "Nouvelle conversation";
    } else if (title) {
      link.textContent = title;
    }

    document.querySelector(".sidebar-empty")?.remove();
    link.classList.add("active");
    conversationList.prepend(link);
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
          assistant.article.removeAttribute("aria-busy");
          renderCitations(assistant.article, event.citations || []);
        } else if (event.type === "error") {
          showAssistantContent(assistant);
          assistant.body.classList.remove("streaming");
          assistant.paragraph.textContent = event.message || "La réponse n’a pas pu être générée.";
          assistant.article.classList.add("error");
          assistant.article.removeAttribute("aria-busy");
        }

        if (followResponse && ["status", "delta", "done", "error"].includes(event.type)) {
          scrollToBottom();
        }
      }
    }
  }

  const navigationEntry = performance.getEntriesByType("navigation")[0];
  if (
    main.dataset.conversationId
    && messages.querySelector(".message")
    && navigationEntry?.type !== "back_forward"
  ) {
    requestAnimationFrame(() => scrollToBottom());
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || send.disabled) return;

    addMessage("user", text);
    const assistant = addMessage("assistant", "");
    assistant.article.setAttribute("aria-busy", "true");
    setAssistantStatus(assistant, "Je vérifie la documentation ETPOS…", "requesting");
    input.value = "";
    resize();
    send.disabled = true;
    form.setAttribute("aria-busy", "true");
    input.focus({ preventScroll: true });
    scrollToBottom("smooth");

    const conversationId = main.dataset.conversationId ? Number(main.dataset.conversationId) : null;
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ message: text, conversation_id: conversationId }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await consumeSSE(response, assistant);
    } catch (error) {
      showAssistantContent(assistant);
      assistant.body.classList.remove("streaming");
      assistant.paragraph.textContent = "La requête n’a pas abouti. Vérifiez votre connexion puis réessayez.";
      assistant.article.classList.add("error");
      assistant.article.removeAttribute("aria-busy");
      console.error(error);
    } finally {
      send.disabled = false;
      form.removeAttribute("aria-busy");
    }
  });
})();
