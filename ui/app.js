// KnowIT-Graphs — 3-panel UI (sidebar · retrieval+diagram · chat)
// Streaming over SSE, lazy mermaid, per-turn retrieval focus.

const $ = (id) => document.getElementById(id);
const els = {
  sessionList: $("session-list"),
  newChatBtn:  $("new-chat-btn"),
  providerBadge: $("provider-badge"),
  indexBadge:  $("index-badge"),

  retrievalBody: $("retrieval-body"),
  diagramPanel: $("diagram-panel"),
  diagramBody:  $("diagram-body"),
  diagramSource: $("diagram-source"),

  messages:    $("messages"),
  composer:    $("composer"),
  input:       $("input"),
  sendBtn:     $("send-btn"),
  stopBtn:     $("stop-btn"),

  inspectorBtn:   $("inspector-btn"),
  inspectorModal: $("inspector-modal"),
  inspectorBody:  $("inspector-body"),
  inspectorClose: $("inspector-close"),
};

// ---------- state ----------
let activeSessionId = null;
let currentController = null;
let userScrolledUp = false;
let sessionPollHandle = null;   // setInterval id for the currently-viewed session
let lastAssistantContent = "";  // dedupe identical poll results

/**
 * turns: per assistant turn index → {
 *   user_text, retrieved, sources, kind_counts, log_detected,
 *   cached, age_s, prompt, diagram, final_text
 * }
 * Lets us re-focus the center panel when the user clicks an older bubble.
 */
const turns = new Map();
let focusedTurn = null;

// ---------- utilities ----------
function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function formatAge(seconds) {
  if (seconds == null) return "?";
  if (seconds < 60)    return `${Math.floor(seconds)}s`;
  if (seconds < 3600)  return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}
function formatTime(d) {
  const h = d.getHours(); const m = String(d.getMinutes()).padStart(2, "0");
  const ampm = h >= 12 ? "PM" : "AM"; const h12 = ((h + 11) % 12) + 1;
  return `${h12}:${m} ${ampm}`;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

// ---------- send/stop button swap ----------
function swapToStopButton() { els.sendBtn.hidden = true; els.stopBtn.hidden = false; }
function swapToSendButton() { els.sendBtn.hidden = false; els.stopBtn.hidden = true; }
function stopCurrentStream() {
  if (currentController) { currentController.abort(); currentController = null; }
}

// ---------- health / sessions ----------
async function refreshHealth() {
  try {
    const h = await api("/api/health");
    els.providerBadge.textContent = `LLM: ${h.provider} · ${h.model}`;
    let idx = h.status === "ok" ? `${h.index_size} chunks indexed` : `Indexing… ${h.index_size}`;
    if (h.cache && h.cache.enabled) {
      idx += ` · cache ${h.cache.entries}/${h.cache.max_entries || "∞"}`;
    }
    els.indexBadge.textContent = idx;
  } catch {
    els.indexBadge.textContent = "Backend offline";
  }
}

async function refreshSessions() {
  const list = await api("/api/sessions");
  els.sessionList.innerHTML = "";
  for (const s of list) {
    const li = document.createElement("li");
    li.dataset.id = s.id;
    if (s.id === activeSessionId) li.classList.add("active");
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = s.title || "New chat";
    const del = document.createElement("button");
    del.className = "del"; del.textContent = "×"; del.title = "Delete";
    del.onclick = (ev) => { ev.stopPropagation(); deleteSession(s.id); };
    li.appendChild(title); li.appendChild(del);
    li.onclick = () => selectSession(s.id);
    els.sessionList.appendChild(li);
  }
}

async function selectSession(id) {
  stopSessionPolling();
  activeSessionId = id;
  lastAssistantContent = "";
  await refreshSessions();
  const data = await api(`/api/sessions/${id}/messages`);
  els.messages.innerHTML = "";
  turns.clear(); focusedTurn = null;
  renderEmptyRetrieval();
  hideDiagram();
  data.messages.forEach((m, i) => {
    if (m.role === "user") renderUserMessage(m.content, m.author || null);
    else renderStaticAssistantMessage(m.content, i);
  });
  renderMermaidIn(els.messages);
  scrollMessagesToBottom(true);

  // Poll while the session is open — background-filled incident sessions
  // update the assistant message in place as the diagnosis streams in.
  startSessionPolling(id);
}

function stopSessionPolling() {
  if (sessionPollHandle) { clearInterval(sessionPollHandle); sessionPollHandle = null; }
}

function startSessionPolling(id) {
  let lastRetrievedLen = 0;
  let promptStored = false;
  const startedAt = Date.now();
  sessionPollHandle = setInterval(async () => {
    if (activeSessionId !== id) { stopSessionPolling(); return; }
    try {
      const data = await api(`/api/sessions/${id}/messages`);
      const msgs = data.messages || [];
      if (!msgs.length) return;
      let lastAssistantIdx = -1;
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].role === "assistant") { lastAssistantIdx = i; break; }
      }
      if (lastAssistantIdx < 0) return;
      const msg = msgs[lastAssistantIdx];

      // Populate retrieval chunk cards (once, when they first arrive)
      const ret = Array.isArray(msg.retrieved) ? msg.retrieved : null;
      if (ret && ret.length && ret.length !== lastRetrievedLen) {
        lastRetrievedLen = ret.length;
        const sources = ret.map((r) => r.source || r.ref || "");
        renderRetrievalPanel({
          retrieved: ret, sources,
          kind_counts: {}, log_detected: null, cached: false,
        });
      }

      // Populate Debug inspector data for incident sessions
      if (msg.prompt_data && !promptStored) {
        promptStored = true;
        turns.set(lastAssistantIdx, {
          ...(turns.get(lastAssistantIdx) || {}),
          prompt: msg.prompt_data,
        });
        focusedTurn = lastAssistantIdx;
      }

      const newContent = msg.content || "";
      const isStillGenerating =
        newContent.includes("⏳") || newContent.trim().endsWith("▊");

      if (newContent !== lastAssistantContent) {
        lastAssistantContent = newContent;
        updateLastAssistantInPlace(newContent, lastAssistantIdx, {
          generating: isStillGenerating,
          elapsed: Math.floor((Date.now() - startedAt) / 1000),
          sessionId: id,
        });
      } else if (isStillGenerating) {
        // Content hasn't changed yet but we're still waiting on Ollama —
        // keep the elapsed counter fresh so the user sees progress
        updateGenerationBadge(lastAssistantIdx, Math.floor((Date.now() - startedAt) / 1000), id);
      }
    } catch {
      // swallow
    }
  }, 2000);
}

function updateGenerationBadge(turnIdx, elapsedSec, sessionId) {
  const bubble = els.messages.querySelector(`.msg.assistant[data-turn="${turnIdx}"]`)
    || els.messages.querySelectorAll(".msg.assistant")[els.messages.querySelectorAll(".msg.assistant").length - 1];
  if (!bubble) return;
  let badge = bubble.querySelector(".incident-progress");
  if (!badge) {
    badge = document.createElement("div");
    badge.className = "incident-progress";
    badge.innerHTML = `
      <span class="ip-pulse"></span>
      <span class="ip-text">Generating · <span class="ip-elapsed"></span>s</span>
      <button class="ip-stop" type="button">■ Stop</button>
    `;
    bubble.querySelector(".msg-body").appendChild(badge);
    badge.querySelector(".ip-stop").onclick = async (e) => {
      e.stopPropagation();
      try {
        await api(`/api/incidents/${sessionId}/cancel`, { method: "POST" });
      } catch {}
    };
  }
  badge.querySelector(".ip-elapsed").textContent = elapsedSec;
}


function updateLastAssistantInPlace(text, turnIdx, extras = {}) {
  let bubble = els.messages.querySelector(`.msg.assistant[data-turn="${turnIdx}"]`);
  if (!bubble) {
    const all = els.messages.querySelectorAll(".msg.assistant");
    bubble = all[all.length - 1];
  }
  if (!bubble) return;
  const body = bubble.querySelector(".msg-body");
  if (!body) return;
  const stripped = stripMermaid(text);
  body.innerHTML = formatMarkdownLite(stripped);
  addCopyButtonsIn(body);
  const diagram = extractMermaid(text);
  if (diagram) renderDiagramPanel(diagram, `From incident diagnosis`);
  renderMermaidIn(body);

  // If still generating, re-attach the progress+stop badge (body was replaced)
  if (extras.generating && extras.sessionId != null) {
    updateGenerationBadge(turnIdx, extras.elapsed || 0, extras.sessionId);
  }
  scrollMessagesToBottom();
}

async function newSession() {
  stopSessionPolling();
  const s = await api("/api/sessions", { method: "POST" });
  activeSessionId = s.id;
  els.messages.innerHTML = "";
  turns.clear(); focusedTurn = null;
  lastAssistantContent = "";
  renderEmptyRetrieval();
  hideDiagram();
  await refreshSessions();
}

async function deleteSession(id) {
  await api(`/api/sessions/${id}`, { method: "DELETE" });
  if (id === activeSessionId) {
    activeSessionId = null; els.messages.innerHTML = "";
    turns.clear(); focusedTurn = null;
    renderEmptyRetrieval(); hideDiagram();
  }
  await refreshSessions();
}

// ---------- markdown rendering ----------
function formatMarkdownLite(text, { stripMermaid = false } = {}) {
  const blocks = [];
  let out = text.replace(/```([\w+-]*)\n([\s\S]*?)```/g, (_, lang, body) => {
    const i = blocks.length;
    blocks.push({ lang: (lang || "").toLowerCase(), body });
    return `\u0000B${i}\u0000`;
  });
  out = out.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  out = out.replace(/\u0000B(\d+)\u0000/g, (_, idx) => {
    const { lang, body } = blocks[+idx];
    const escBody = body.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    if (lang === "mermaid") {
      if (stripMermaid) return "";   // diagram is rendered separately in the center panel
      return `<pre class="mermaid">${escBody}</pre>`;
    }
    return `<pre><code class="lang-${lang}">${escBody}</code></pre>`;
  });
  out = out.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  out = out.replace(/(Sources:\s*\n[\s\S]*)$/m, '<div class="sources">$1</div>');
  return out;
}

function extractMermaid(text) {
  const m = text.match(/```mermaid\s*\n([\s\S]*?)```/);
  return m ? m[1].trim() : null;
}

// Lazy-load mermaid only when a diagram actually needs rendering
let mermaidLoadPromise = null;
function loadMermaid() {
  if (mermaidLoadPromise) return mermaidLoadPromise;
  mermaidLoadPromise = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js";
    s.onload = () => {
      if (window.mermaid) {
        mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict" });
      }
      resolve();
    };
    s.onerror = () => reject(new Error("mermaid CDN unreachable"));
    document.head.appendChild(s);
  });
  return mermaidLoadPromise;
}

async function renderMermaidIn(container) {
  const nodes = container.querySelectorAll("pre.mermaid:not([data-processed])");
  if (!nodes.length) return;
  try {
    await loadMermaid();
    if (!window.mermaid) return;
    await mermaid.run({ nodes: Array.from(nodes) });
  } catch (e) {
    nodes.forEach((n) => {
      if (!n.getAttribute("data-processed")) {
        n.classList.add("mermaid-error"); n.setAttribute("data-processed", "true");
      }
    });
    console.warn("Mermaid render failed:", e);
  }
}

function addCopyButtonsIn(container) {
  container.querySelectorAll("pre:not(.mermaid):not([data-copy-added])").forEach((pre) => {
    pre.setAttribute("data-copy-added", "true");
    const btn = document.createElement("button");
    btn.className = "copy-btn"; btn.textContent = "Copy"; btn.title = "Copy";
    btn.onclick = async (e) => {
      e.stopPropagation();
      const text = pre.querySelector("code")?.textContent || pre.textContent;
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "✓ Copied"; btn.classList.add("copied");
        setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("copied"); }, 1800);
      } catch {}
    };
    pre.appendChild(btn);
  });
}

// ---------- scroll management ----------
function isNearBottom() {
  const m = els.messages.parentElement;   // #chat-pane
  return m.scrollHeight - m.scrollTop - m.clientHeight < 120;
}
function scrollMessagesToBottom(force = false) {
  const pane = els.messages.parentElement;
  if (force || !userScrolledUp) pane.scrollTop = pane.scrollHeight;
}
els.messages.parentElement.addEventListener("scroll", () => {
  userScrolledUp = !isNearBottom();
});

// ---------- retrieval panel ----------
function renderEmptyRetrieval() {
  els.retrievalBody.innerHTML = `
    <div class="empty-state">
      <h3>Ask me about the KG platform</h3>
      <p>Paste an Airflow / Spark log, or ask a question. Retrieved context will appear here.</p>
    </div>`;
}

function renderRetrievalStatus(text, pulsing = true) {
  els.retrievalBody.innerHTML = "";
  const s = document.createElement("div");
  s.className = "retrieval-status" + (pulsing ? " pulsing" : "");
  s.textContent = text;
  els.retrievalBody.appendChild(s);
}

function renderRetrievalPanel(turn) {
  const body = els.retrievalBody;
  body.innerHTML = "";

  const retrieved = turn.retrieved || [];
  const sources = turn.sources || [];
  const count = retrieved.length || sources.length;

  // Header
  const head = document.createElement("header");
  head.className = "panel-subhead";
  const headLeft = document.createElement("div");
  const preview = sources.slice(0, 3).map((s) => s).join(", ") + (sources.length > 3 ? ", …" : "");
  headLeft.innerHTML = `
    <div class="panel-title-main">Retrieved context (${count} chunk${count === 1 ? "" : "s"})</div>
    <div class="panel-title-sub">Source: ${escapeHtml(preview || "—")}</div>
  `;
  const btn = document.createElement("button");
  btn.className = "btn-ghost"; btn.textContent = "⊞ View all chunks";
  head.appendChild(headLeft); head.appendChild(btn);
  body.appendChild(head);

  // Status line (cached / log detected)
  if (turn.cached || turn.log_detected) {
    const bar = document.createElement("div");
    bar.className = "retrieval-status";
    const bits = [];
    if (turn.cached) bits.push(`⚡ cached · ${formatAge(turn.age_s)} ago`);
    if (turn.log_detected) bits.push(`log: ${turn.log_detected}`);
    bar.textContent = bits.join("  ·  ");
    body.appendChild(bar);
  }

  // Cards
  const list = document.createElement("div");
  list.className = "chunk-cards";
  const SHOW = 2;
  retrieved.forEach((r, i) => {
    const card = document.createElement("article");
    const kindClass = (r.kind || "other").toLowerCase().replace(/[^a-z0-9]/g, "");
    card.className = `chunk-card chunk-${kindClass}`;
    if (i >= SHOW) card.classList.add("collapsed");
    card.innerHTML = `
      <header class="chunk-head">
        <span class="chunk-kind">${escapeHtml(r.kind || "?")}</span>
        <span class="chunk-src">📄 ${escapeHtml(r.source || r.ref || "")}</span>
        <span class="chunk-score">Score ${Number(r.score || 0).toFixed(3)}</span>
      </header>
      <pre class="chunk-body">${escapeHtml(r.snippet || r.text || "")}</pre>
    `;
    list.appendChild(card);
  });
  body.appendChild(list);

  btn.onclick = () => {
    list.classList.toggle("all-expanded");
    btn.textContent = list.classList.contains("all-expanded") ? "⊟ Collapse" : "⊞ View all chunks";
  };
}

// ---------- diagram panel ----------
async function renderDiagramPanel(mermaidSrc, sourceLabel) {
  if (!mermaidSrc) { hideDiagram(); return; }
  els.diagramPanel.hidden = false;
  els.diagramSource.textContent = sourceLabel || "";
  els.diagramBody.innerHTML = `<pre class="mermaid">${escapeHtml(mermaidSrc)}</pre>`;
  await renderMermaidIn(els.diagramBody);
}
function hideDiagram() {
  els.diagramPanel.hidden = true;
  els.diagramBody.innerHTML = "";
  els.diagramSource.textContent = "";
}

// ---------- message rendering ----------
function renderUserMessage(text, author = null) {
  const div = document.createElement("div");
  div.className = "msg user" + (author ? ` authored-by-${author}` : "");
  if (author === "airflow") {
    div.innerHTML = `<div class="authored-head">🤖 Airflow</div><div class="authored-body"></div>`;
    div.querySelector(".authored-body").textContent = text;
  } else {
    div.textContent = text;
  }
  els.messages.appendChild(div);
  scrollMessagesToBottom();
  return div;
}

function renderStaticAssistantMessage(text, turnIdx) {
  const { el, body } = createAssistantBubble(turnIdx);
  const stripped = stripMermaid(text);
  body.innerHTML = formatMarkdownLite(stripped);
  addCopyButtonsIn(body);
  els.messages.appendChild(el);
  // If the historic message had a mermaid block, pull it into the diagram panel when focused
  const diagram = extractMermaid(text);
  if (diagram) {
    turns.set(turnIdx, { ...turns.get(turnIdx), diagram, final_text: text });
  }
  return el;
}

function stripMermaid(text) {
  return text.replace(/```mermaid\s*\n[\s\S]*?```/g, "").trim();
}

function createAssistantBubble(turnIdx) {
  const el = document.createElement("div");
  el.className = "msg assistant";
  el.dataset.turn = turnIdx;

  const head = document.createElement("header");
  head.className = "msg-head";
  head.innerHTML = `
    <div class="avatar">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="5" cy="6" r="2"/><circle cx="19" cy="6" r="2"/><circle cx="12" cy="18" r="2"/>
        <line x1="7" y1="7" x2="11" y2="17"/><line x1="17" y1="7" x2="13" y2="17"/><line x1="7" y1="6" x2="17" y2="6"/>
      </svg>
    </div>
    <span class="who">KnowIT-Graphs</span>
    <span class="ts">${formatTime(new Date())}</span>
  `;

  const body = document.createElement("div");
  body.className = "msg-body";

  const actions = document.createElement("div");
  actions.className = "msg-actions";
  actions.innerHTML = `
    <button class="fb" data-fb="up">👍 Helpful</button>
    <button class="fb" data-fb="down">👎 Not helpful</button>
  `;

  el.appendChild(head); el.appendChild(body); el.appendChild(actions);

  el.onclick = (e) => {
    if (e.target.closest(".fb, button.copy-btn")) return;  // don't re-focus on action click
    setFocusedTurn(turnIdx);
  };

  actions.querySelector('[data-fb="up"]').onclick  = (e) => { e.stopPropagation(); submitFeedback(turnIdx, true,  actions); };
  actions.querySelector('[data-fb="down"]').onclick = (e) => { e.stopPropagation(); submitFeedback(turnIdx, false, actions); };

  return { el, head, body, actions };
}

async function submitFeedback(turnIdx, helpful, actionsEl) {
  if (!activeSessionId) return;
  try {
    await api("/api/feedback", {
      method: "POST",
      body: JSON.stringify({
        session_id: activeSessionId,
        message_index: turnIdx,
        helpful,
      }),
    });
    actionsEl.querySelectorAll(".fb").forEach((b) => { b.disabled = true; b.classList.remove("fb-given"); });
    const clicked = actionsEl.querySelector(helpful ? '[data-fb="up"]' : '[data-fb="down"]');
    clicked.classList.add("fb-given", helpful ? "helpful" : "not-helpful");
  } catch (e) {
    console.warn("Feedback failed:", e);
  }
}

// ---------- focus management ----------
function setFocusedTurn(turnIdx) {
  const turn = turns.get(turnIdx);
  if (!turn) return;
  focusedTurn = turnIdx;
  document.querySelectorAll(".msg.active-turn").forEach((n) => n.classList.remove("active-turn"));
  const bubble = els.messages.querySelector(`.msg.assistant[data-turn="${turnIdx}"]`);
  if (bubble) bubble.classList.add("active-turn");
  renderRetrievalPanel(turn);
  renderDiagramPanel(turn.diagram, turn.diagram ? `From answer on turn ${turnIdx}` : "");
}

// ---------- inspector modal ----------
function renderInspectorModal(payload) {
  const host = els.inspectorBody;
  host.innerHTML = "";
  if (!payload) {
    host.innerHTML = "<p>No prompt captured for the focused turn.</p>";
    return;
  }
  host.innerHTML = `
    <div class="inspect-heading">Model</div>
    <pre class="inspect-pre">${escapeHtml(payload.model?.provider || "?")} / ${escapeHtml(payload.model?.model || "?")}</pre>
    <div class="inspect-heading">Augmented user message</div>
    <pre class="inspect-pre">${escapeHtml(payload.augmented_user || "—")}</pre>
    <div class="inspect-heading">System prompt</div>
    <pre class="inspect-pre">${escapeHtml(payload.system || "—")}</pre>
  `;
}

els.inspectorBtn.onclick = () => {
  const turn = focusedTurn != null ? turns.get(focusedTurn) : null;
  renderInspectorModal(turn?.prompt || null);
  els.inspectorModal.hidden = false;
};
els.inspectorClose.onclick = () => { els.inspectorModal.hidden = true; };
els.inspectorModal.addEventListener("click", (e) => {
  if (e.target === els.inspectorModal) els.inspectorModal.hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!els.inspectorModal.hidden) { els.inspectorModal.hidden = true; return; }
    if (currentController) stopCurrentStream();
  }
});

// ---------- SSE parsing ----------
function parseSSE(buffer) {
  const events = [];
  const frames = buffer.split("\n\n");
  const leftover = frames.pop();
  for (const frame of frames) {
    if (!frame.trim()) continue;
    let event = "message";
    const dataLines = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith("event: ")) event = line.slice(7).trim();
      else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
    }
    try { events.push({ event, payload: JSON.parse(dataLines.join("\n")) }); } catch {}
  }
  return [events, leftover];
}

// ---------- send ----------
async function sendMessage(text) {
  if (currentController) stopCurrentStream();

  renderUserMessage(text);
  userScrolledUp = false;

  // Turn index = current length of messages AFTER adding the user message.
  // The assistant reply will be at this index in the session's message list.
  // (The server also appends user+assistant in the same order.)
  const turnIdx = els.messages.querySelectorAll(".msg").length;  // assistant index in session
  const { el: bubble, head, body, actions } = createAssistantBubble(turnIdx);
  els.messages.appendChild(bubble);
  bubble.classList.add("active-turn");
  document.querySelectorAll(".msg.active-turn").forEach((n) => {
    if (n !== bubble) n.classList.remove("active-turn");
  });
  focusedTurn = turnIdx;
  scrollMessagesToBottom(true);

  turns.set(turnIdx, { user_text: text, retrieved: [], sources: [], diagram: null });

  // Reset center panels for the new turn
  renderRetrievalStatus("Searching handbook, user guide + repo…", true);
  hideDiagram();

  currentController = new AbortController();
  swapToStopButton();

  let accumulated = "";
  let firstTokenAt = null;
  let renderScheduled = false;
  function scheduleBodyRender() {
    if (renderScheduled) return;
    renderScheduled = true;
    requestAnimationFrame(() => {
      renderScheduled = false;
      const stripped = stripMermaid(accumulated);
      const cursor = firstTokenAt ? '<span class="typing-cursor">▊</span>' : "";
      body.innerHTML = formatMarkdownLite(stripped) + cursor;
      scrollMessagesToBottom();
    });
  }

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: activeSessionId, message: text }),
      signal: currentController.signal,
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(detail.detail || `HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let streamError = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const [events, rest] = parseSSE(buffer);
      buffer = rest;

      for (const { event, payload } of events) {
        switch (event) {
          case "meta":
            if (payload.session_id) activeSessionId = payload.session_id;
            break;
          case "status": {
            if (firstTokenAt) break;
            const t = turns.get(turnIdx);
            if (t && t.prompt) {
              // Retrieval already rendered — show generation status inside the bubble
              body.innerHTML = `<span class="bubble-status">${escapeHtml(payload.text)}…</span>`;
            } else if (focusedTurn === turnIdx) {
              // Pre-retrieval — show in the center panel
              renderRetrievalStatus(payload.text, true);
            }
            break;
          }
          case "context": {
            const t = turns.get(turnIdx);
            t.sources = payload.sources || [];
            t.kind_counts = payload.kind_counts || {};
            t.log_detected = payload.log_detected || null;
            t.cached = !!payload.cached;
            t.age_s = payload.age_seconds || 0;
            if (payload.log_detected) {
              const b = document.createElement("span");
              b.className = "log-badge"; b.textContent = `log: ${payload.log_detected}`;
              head.appendChild(b);
            }
            if (payload.cached) {
              const c = document.createElement("span");
              c.className = "cache-badge";
              c.textContent = `⚡ cached · ${formatAge(payload.age_seconds)} ago`;
              head.appendChild(c);
            }
            break;
          }
          case "prompt": {
            const t = turns.get(turnIdx);
            t.prompt = payload;
            t.retrieved = payload.retrieved || [];
            // As soon as we have retrieved detail, render the cards
            if (focusedTurn === turnIdx) renderRetrievalPanel(t);
            break;
          }
          case "token":
            if (firstTokenAt === null) firstTokenAt = Date.now();
            accumulated += payload.text;
            scheduleBodyRender();
            break;
          case "final": {
            accumulated = payload.text;
            const stripped = stripMermaid(accumulated);
            body.innerHTML = formatMarkdownLite(stripped);
            addCopyButtonsIn(body);
            const diagram = extractMermaid(accumulated);
            const t = turns.get(turnIdx);
            t.diagram = diagram; t.final_text = accumulated;
            if (focusedTurn === turnIdx) {
              renderDiagramPanel(diagram, diagram ? `From answer on turn ${turnIdx}` : "");
            }
            scrollMessagesToBottom();
            break;
          }
          case "error":
            streamError = payload.message;
            break;
        }
      }
    }

    if (streamError) {
      bubble.remove();
      const err = document.createElement("div");
      err.className = "msg error"; err.textContent = `Error: ${streamError}`;
      els.messages.appendChild(err);
    }
    await refreshSessions();
  } catch (e) {
    if (e.name === "AbortError") {
      // Keep whatever we streamed, add a "stopped" marker
      if (!accumulated) {
        bubble.remove();
      } else {
        const stripped = stripMermaid(accumulated);
        body.innerHTML = formatMarkdownLite(stripped);
        addCopyButtonsIn(body);
        const marker = document.createElement("span");
        marker.className = "stopped-marker"; marker.textContent = "■ stopped · not cached";
        head.appendChild(marker);
      }
      await refreshSessions();
    } else {
      bubble.remove();
      const err = document.createElement("div");
      err.className = "msg error"; err.textContent = `Error: ${e.message}`;
      els.messages.appendChild(err);
    }
  } finally {
    currentController = null;
    swapToSendButton();
  }
}

// ---------- input wiring ----------
els.composer.onsubmit = (e) => {
  e.preventDefault();
  if (currentController) return;
  const text = els.input.value.trim();
  if (!text) return;
  els.input.value = "";
  els.input.style.height = "";
  sendMessage(text);
};

els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    if (currentController) return;
    els.composer.requestSubmit();
  }
});
els.input.addEventListener("input", () => {
  els.input.style.height = "";
  els.input.style.height = Math.min(200, els.input.scrollHeight) + "px";
});

els.stopBtn.onclick = stopCurrentStream;
els.newChatBtn.onclick = newSession;

// ---------- boot ----------
(async () => {
  await refreshHealth();
  await refreshSessions();
  setInterval(refreshHealth, 15000);
})();
