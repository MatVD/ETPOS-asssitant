(() => {
  const form = document.getElementById("chat-form");
  const input = document.getElementById("message-input");
  const send = document.getElementById("send-button");
  const messages = document.getElementById("messages");
  const main = document.querySelector(".chat-main");
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  document.querySelectorAll(".suggestion").forEach((button) => {
    button.addEventListener("click", () => {
      if (!input) return;
      input.value = button.textContent || "";
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

  function addMessage(role, text) {
    document.getElementById("empty-state")?.remove();
    const article = document.createElement("article");
    article.className = `message ${role}`;
    const label = document.createElement("div");
    label.className = "message-label";
    label.textContent = role === "user" ? "Vous" : "ETPOS Assistant";
    const body = document.createElement("div");
    body.className = "message-body";
    const p = document.createElement("p");
    p.textContent = text;
    body.appendChild(p);
    article.append(label, body);
    messages.appendChild(article);
    article.scrollIntoView({ behavior: "smooth", block: "end" });
    return { article, body, paragraph: p };
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
        if (event.type === "meta") {
          main.dataset.conversationId = String(event.conversation_id);
          if (location.pathname === "/") history.replaceState({}, "", `/c/${event.conversation_id}`);
        } else if (event.type === "status") {
          assistant.paragraph.textContent = event.text || "";
          assistant.paragraph.classList.add("message-status");
        } else if (event.type === "delta") {
          if (assistant.paragraph.classList.contains("message-status")) {
            assistant.paragraph.classList.remove("message-status");
            assistant.paragraph.textContent = "";
          }
          accumulated += event.text || "";
          assistant.paragraph.textContent = accumulated;
          assistant.article.scrollIntoView({ behavior: "smooth", block: "end" });
        } else if (event.type === "done") {
          assistant.paragraph.classList.remove("message-status");
          assistant.body.innerHTML = event.html || "";
          renderCitations(assistant.article, event.citations || []);
        } else if (event.type === "error") {
          assistant.paragraph.classList.remove("message-status");
          assistant.paragraph.textContent = event.message || "Erreur de génération.";
          assistant.article.classList.add("error");
        }
      }
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    addMessage("user", text);
    const assistant = addMessage("assistant", "");
    input.value = "";
    resize();
    send.disabled = true;

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
      assistant.paragraph.textContent = "La requête a échoué. Vérifie la connexion et la configuration de l'assistant.";
      assistant.article.classList.add("error");
      console.error(error);
    } finally {
      send.disabled = false;
      input.focus();
    }
  });
})();
