/* ka-sfskills · chat panel
 *
 * Drives a /chat/ws WebSocket connection that fronts a real ``claude``
 * subprocess. The protocol is documented in ``scripts/dashboard/chat.py``.
 *
 * Loaded as an ES module from chat.html (inside the side-panel iframe
 * and on the full-page /chat route). Three behavior groups have been
 * extracted into their own modules:
 *
 *   chat/attachments.js — paste/drop images, file-path injection
 *   chat/slash.js       — leading-slash command autocomplete
 *   chat/history.js     — past-sessions picker + transcript replay
 *
 * Everything else (WebSocket lifecycle, message rendering, project /
 * mode / model controls, the new-chat reset path) stays here.
 */
import { initAttachments } from "./chat/attachments.js";
import { initSlash } from "./chat/slash.js";
import { initHistory } from "./chat/history.js";

/** @template {HTMLElement} T @param {string} id @returns {T} */
// @ts-expect-error -- callers know which element type each id refers to
const $ = (id) => document.getElementById(id);

const els = {
  /** @type {HTMLElement} */         shell: $("chat-shell"),
  /** @type {HTMLElement} */         sessionMeta: $("chat-session-meta"),
  /** @type {HTMLSelectElement} */   project: $("chat-project"),
  /** @type {HTMLSelectElement} */   mode: $("chat-mode"),
  /** @type {HTMLSelectElement} */   model: $("chat-model"),
  /** @type {HTMLButtonElement} */   newBtn: $("chat-new"),
  /** @type {HTMLButtonElement} */   historyBtn: $("chat-history"),
  /** @type {HTMLElement} */         historyPanel: $("chat-history-panel"),
  /** @type {HTMLButtonElement} */   attachBtn: $("chat-attach-btn"),
  /** @type {HTMLInputElement} */    attachInput: $("chat-attach-input"),
  /** @type {HTMLElement} */         attachmentsTray: $("chat-attachments"),
  /** @type {HTMLElement} */         bypassBanner: $("chat-banner-bypass"),
  /** @type {HTMLElement} */         messages: $("chat-messages"),
  /** @type {HTMLElement} */         empty: $("chat-empty"),
  /** @type {HTMLFormElement} */     composer: $("chat-composer"),
  /** @type {HTMLTextAreaElement} */ input: $("chat-input"),
  /** @type {HTMLButtonElement} */   send: $("chat-send"),
};

if (typeof marked !== "undefined") {
  marked.setOptions({ gfm: true, breaks: false });
}

/** @type {ChatState} */
const state = {
  ws: null,
  sessionId: null,
  projectPath: null,
  mode: "bypassPermissions",
  model: "sonnet",
  currentAssistant: null,    // DOM node we're appending into for streaming
  currentText: "",           // raw markdown accumulator
  autoscroll: true,
  cost: 0,
  // Reconnect lifecycle. `stopped` flips to true when the user explicitly
  // tears down (e.g. a future "Disconnect" button); right now nothing
  // sets it, so the panel stays self-healing as long as the page is open.
  reconnect: { attempts: 0, timer: null, stopped: false },
  ready: false,              // true once we've seen a 'ready' control event
};

// ------------------------------------------------------------------ //
// Project picker — populated from /api/chat/projects on load          //
// ------------------------------------------------------------------ //

const GLOBAL_PROJECT_KEY = "ka-sfskills:current-project";

function readGlobalProject() {
  try { return localStorage.getItem(GLOBAL_PROJECT_KEY) || ""; }
  catch (_) { return ""; }
}

function writeGlobalProject(path) {
  try { localStorage.setItem(GLOBAL_PROJECT_KEY, path); }
  catch (_) { /* private mode etc. */ }
}

async function loadProjects() {
  try {
    const res = await fetch("/api/chat/projects", { credentials: "same-origin" });
    const body = await res.json();
    const projects = body.projects || [];
    els.project.innerHTML = "";
    for (const p of projects) {
      const opt = document.createElement("option");
      opt.value = p.path;
      opt.textContent = (p.pinned ? "★ " : "") + p.label;
      opt.title = p.path;
      els.project.appendChild(opt);
    }
    // Honor the global project selection if it still resolves to a
    // known project; otherwise fall back to the first (pinned) one.
    const stored = readGlobalProject();
    const knownPaths = projects.map(p => p.path);
    const chosen = knownPaths.includes(stored) ? stored : (knownPaths[0] || "");
    if (chosen) {
      els.project.value = chosen;
      state.projectPath = chosen;
      writeGlobalProject(chosen);
    }
  } catch (err) {
    els.sessionMeta.textContent = "couldn't load projects: " + (err.message || err);
  }
}

// ------------------------------------------------------------------ //
// WebSocket lifecycle                                                 //
// ------------------------------------------------------------------ //

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/chat/ws`;
}

function connect() {
  closeWS();
  clearReconnectTimer();
  state.ready = false;
  state.reconnect.attempts += 1;
  setOfflineBanner(null);
  els.sessionMeta.textContent = state.reconnect.attempts > 1
    ? "reconnecting…"
    : "connecting…";
  setLoadingState("connecting");
  state.ws = new WebSocket(wsUrl());
  state.ws.addEventListener("open", () => {
    // Reset the backoff once the socket is open. We treat 'open' as
    // success for reconnect bookkeeping — the init handshake that
    // follows can still fail, in which case 'ready' won't fire and
    // the user will see the spawning state stall rather than a flapping
    // reconnect loop.
    state.reconnect.attempts = 0;
    els.sessionMeta.textContent = "spawning Claude…";
    setLoadingState("spawning");
    // Init the session. The backend looks up the last session for this
    // project and resumes it unless we explicitly pass resume:false
    // (which "New chat" does — see the button handler below).
    sendWS({
      type: "init",
      project_path: state.projectPath,
      permission_mode: state.mode,
      model: state.model,
      resume: state.skipResumeOnNextInit !== true,
    });
    state.skipResumeOnNextInit = false;
  });
  state.ws.addEventListener("message", (e) => {
    try {
      handleEvent(JSON.parse(e.data));
    } catch (err) {
      appendErrorBubble("malformed message from server: " + (err.message || err));
    }
  });
  state.ws.addEventListener("close", () => {
    setInputEnabled(false);
    const wasReady = state.ready;
    state.ready = false;
    if (state.reconnect.stopped) {
      els.sessionMeta.textContent = "disconnected";
      return;
    }
    // If we never made it to ready, treat this attempt as a failed
    // connect for backoff purposes; otherwise it's a mid-session drop.
    if (wasReady) {
      setOfflineBanner("Connection lost — reconnecting…");
    } else {
      setOfflineBanner("Couldn't reach dashboard server — retrying…");
    }
    scheduleReconnect();
  });
  state.ws.addEventListener("error", () => {
    // Browser doesn't expose error detail; the close event will fire
    // right after and drive the reconnect. We avoid appending a bubble
    // here because a transient disconnect would otherwise spam the
    // transcript with errors on each retry.
  });
}

function closeWS() {
  if (state.ws) {
    // Detach handlers before closing so a delayed `open` from the
    // previous attempt can't fire its init handler against a brand-new
    // socket. This was a real race during rapid reconnect loops.
    state.ws.onopen = null;
    state.ws.onmessage = null;
    state.ws.onclose = null;
    state.ws.onerror = null;
    if (state.ws.readyState <= 1) {
      try { state.ws.close(); } catch (_) { /* ignore */ }
    }
  }
  state.ws = null;
}

function clearReconnectTimer() {
  if (state.reconnect.timer) {
    clearTimeout(state.reconnect.timer);
    state.reconnect.timer = null;
  }
}

function scheduleReconnect() {
  clearReconnectTimer();
  // 1s, 2s, 4s, 8s, 16s, 30s (capped). Each retry doubles up to 30s.
  const n = state.reconnect.attempts;
  const delay = Math.min(30000, 1000 * Math.pow(2, Math.max(0, n - 1)));
  state.reconnect.timer = setTimeout(connect, delay);
}

function setOfflineBanner(msg) {
  let banner = $("chat-offline-banner");
  if (!msg) {
    if (banner) banner.hidden = true;
    return;
  }
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "chat-offline-banner";
    banner.className = "chat-offline-banner";
    banner.textContent = msg;
    // Insert right above the messages list so it's visible without
    // shifting the composer.
    els.messages.parentNode.insertBefore(banner, els.messages);
  } else {
    banner.textContent = msg;
    banner.hidden = false;
  }
}

function setLoadingState(phase /* "connecting" | "spawning" | null */) {
  if (!els.empty) return;
  let loader = els.empty.querySelector(".chat-empty-loader");
  if (phase === null) {
    if (loader) loader.remove();
    els.empty.classList.remove("chat-empty-loading");
    return;
  }
  if (els.empty.dataset.cleared) return; // user already has messages
  els.empty.classList.add("chat-empty-loading");
  if (!loader) {
    loader = document.createElement("div");
    loader.className = "chat-empty-loader";
    els.empty.appendChild(loader);
  }
  loader.textContent = phase === "spawning"
    ? "Starting Claude…"
    : "Connecting…";
}

/** @param {WSClientMessage} payload */
function sendWS(payload) {
  if (!state.ws || state.ws.readyState !== 1) return;
  state.ws.send(JSON.stringify(payload));
}

// ------------------------------------------------------------------ //
// Event handlers                                                      //
// ------------------------------------------------------------------ //

/** @param {WSServerMessage} evt */
function handleEvent(evt) {
  const t = evt.type;

  if (t === "control") {
    if (evt.subtype === "ready") {
      const newSession = state.sessionId !== evt.session_id;
      state.sessionId = evt.session_id;
      state.projectPath = evt.project_path;
      state.mode = evt.permission_mode;
      state.model = evt.model;
      state.ready = true;
      state.reconnect.attempts = 0;
      // Cost accumulates per conversation. Reset on new session so
      // switching projects / models / history doesn't carry over the
      // prior conversation's total.
      if (newSession) state.cost = 0;
      setOfflineBanner(null);
      setLoadingState(null);
      updateSessionMeta();
      updateBypassBanner();
      setInputEnabled(true, { focus: newSession });
      clearEmptyState();
      // Only announce a resume / fresh-start when the session id we
      // landed on is different from what we already had. A flapping
      // reconnect against the same session re-fires the ready event,
      // and we don't want to spam the transcript with one note per
      // attempt.
      if (newSession) {
        if (evt.stale_resume_recovered) {
          appendSystemNote("Previous session was lost — starting a fresh one.");
        } else if (evt.resumed) {
          appendSystemNote("Resumed previous session for this project.");
          // Replay prior turns so the panel doesn't look empty after a
          // reload. set_session triggers its own replay via the
          // historyPicker.switchTo path; this handles the initial
          // boot resume + auto-recover paths. We also track which
          // session we last replayed so the switchTo replay doesn't
          // race against this one and double-render.
          if (els.messages.children.length <= 1 &&
              state.replayedSessionId !== state.sessionId) {
            state.replayedSessionId = state.sessionId;
            historyPicker.replay(state.sessionId);
          }
        }
      }
    } else if (evt.subtype === "error") {
      appendErrorBubble(evt.error || "unknown error");
    } else if (evt.subtype === "closed") {
      setInputEnabled(false);
    }
    return;
  }

  if (t === "system") {
    // system.init / hook / status — capture session id if not set yet
    // and otherwise stay quiet. The /live page already surfaces hook
    // events, so we don't render them here.
    if (evt.subtype === "init" && !state.sessionId) {
      state.sessionId = evt.session_id;
      updateSessionMeta();
    }
    return;
  }

  if (t === "stream_event") {
    handleStreamEvent(evt.event || {});
    return;
  }

  if (t === "assistant") {
    // End-of-turn assistant payload. If we've been streaming, the
    // content already lives in the current bubble; finalize and
    // re-render. If no partial messages came through (older claude
    // versions), populate the bubble from content[].
    const content = (evt.message || {}).content || [];
    if (!state.currentAssistant) {
      state.currentAssistant = appendBubble("assistant");
      state.currentText = "";
    }
    let textJoined = "";
    for (const block of content) {
      if (block && block.type === "text" && typeof block.text === "string") {
        textJoined += block.text;
      }
    }
    if (textJoined && !state.currentText) {
      state.currentText = textJoined;
    }
    renderAssistantMarkdown();
    finalizeAssistantBubble();
    return;
  }

  if (t === "result") {
    addResultChip(evt);
    finalizeAssistantBubble();
    return;
  }
}

function handleStreamEvent(se) {
  const sub = se.type;
  if (sub === "content_block_start") {
    const block = se.content_block || {};
    if (block.type === "text") {
      if (!state.currentAssistant) {
        state.currentAssistant = appendBubble("assistant");
        state.currentText = "";
      }
    } else if (block.type === "tool_use") {
      if (!state.currentAssistant) {
        state.currentAssistant = appendBubble("assistant");
        state.currentText = "";
      }
      // C4c will flesh this out with proper args accumulation; v1 just
      // shows a chip so users know a tool fired.
      const card = document.createElement("div");
      card.className = "chat-tool-card";
      card.dataset.toolIndex = String(se.index);
      card.textContent = `↳ ${block.name || "tool"}`;
      state.currentAssistant.appendChild(card);
    } else if (block.type === "thinking") {
      if (!state.currentAssistant) {
        state.currentAssistant = appendBubble("assistant");
        state.currentText = "";
      }
      const card = document.createElement("details");
      card.className = "chat-thinking";
      const summary = document.createElement("summary");
      summary.textContent = "thinking…";
      card.appendChild(summary);
      state.currentAssistant.appendChild(card);
    }
  } else if (sub === "content_block_delta") {
    const delta = se.delta || {};
    if (delta.type === "text_delta" && typeof delta.text === "string") {
      if (!state.currentAssistant) {
        state.currentAssistant = appendBubble("assistant");
        state.currentText = "";
      }
      state.currentText += delta.text;
      renderAssistantMarkdown();
    } else if (delta.type === "thinking_delta" && typeof delta.thinking === "string") {
      const card = state.currentAssistant?.querySelector(".chat-thinking");
      if (card) {
        let body = card.querySelector(".chat-thinking-body");
        if (!body) {
          body = document.createElement("div");
          body.className = "chat-thinking-body";
          card.appendChild(body);
        }
        body.textContent = (body.textContent || "") + delta.thinking;
      }
    }
  } else if (sub === "message_stop" || sub === "content_block_stop") {
    // No-op for now; we finalize on assistant / result events.
  }
}

// ------------------------------------------------------------------ //
// DOM helpers                                                         //
// ------------------------------------------------------------------ //

function clearEmptyState() {
  if (els.empty && !els.empty.dataset.cleared) {
    els.empty.dataset.cleared = "1";
    els.empty.style.display = "none";
  }
}

/**
 * Append a new bubble to the message list and return its inner container.
 * @param {"user" | "assistant"} kind
 * @returns {HTMLElement}
 */
function appendBubble(kind) {
  clearEmptyState();
  const wrap = document.createElement("div");
  wrap.className = `chat-bubble chat-bubble-${kind}`;
  const inner = document.createElement("div");
  inner.className = "chat-bubble-inner";
  wrap.appendChild(inner);
  els.messages.appendChild(wrap);
  maybeScroll();
  return inner;
}

function appendErrorBubble(text) {
  clearEmptyState();
  const wrap = document.createElement("div");
  wrap.className = "chat-bubble chat-bubble-error";
  const inner = document.createElement("div");
  inner.className = "chat-bubble-inner";
  inner.textContent = "✗ " + text;
  wrap.appendChild(inner);
  els.messages.appendChild(wrap);
  maybeScroll();
}

function renderAssistantMarkdown() {
  if (!state.currentAssistant) return;
  // Find the text container inside the bubble (it's the first text
  // node we create on demand; tool cards live alongside it).
  let textEl = state.currentAssistant.querySelector(".chat-bubble-text");
  if (!textEl) {
    textEl = document.createElement("div");
    textEl.className = "chat-bubble-text markdown";
    // Insert before any tool cards so layout reads top-to-bottom.
    state.currentAssistant.insertBefore(textEl, state.currentAssistant.firstChild);
  }
  if (typeof marked !== "undefined") {
    try {
      textEl.innerHTML = marked.parse(state.currentText);
    } catch (_) {
      textEl.textContent = state.currentText;
    }
  } else {
    textEl.textContent = state.currentText;
  }
  maybeScroll();
}

function addResultChip(evt) {
  const cost = typeof evt.total_cost_usd === "number" ? evt.total_cost_usd : 0;
  state.cost += cost;
  if (state.currentAssistant) {
    const chip = document.createElement("div");
    chip.className = "chat-result-chip";
    const dur = (evt.duration_ms || 0) / 1000;
    chip.textContent = `${dur.toFixed(1)}s · $${cost.toFixed(4)}`;
    if (evt.is_error) chip.classList.add("error");
    state.currentAssistant.appendChild(chip);
  }
  updateSessionMeta();
}

function finalizeAssistantBubble() {
  state.currentAssistant = null;
  state.currentText = "";
}

function maybeScroll() {
  if (!state.autoscroll) return;
  requestAnimationFrame(() => {
    els.messages.scrollTop = els.messages.scrollHeight;
  });
}

function setInputEnabled(on, { focus = false } = {}) {
  els.input.disabled = !on;
  els.send.disabled = !on;
  // Only steal focus when the caller asks for it (first ready of a
  // session). On reconnect we leave the user's focus alone so a
  // flapping connection doesn't yank them out of whatever they're
  // doing.
  if (on && focus) els.input.focus();
}

function updateSessionMeta() {
  const parts = [];
  if (state.sessionId) parts.push("session " + state.sessionId.slice(0, 8));
  if (state.cost) parts.push("$" + state.cost.toFixed(4) + " spent");
  els.sessionMeta.textContent = parts.join(" · ") || "ready";
}

function updateBypassBanner() {
  els.bypassBanner.hidden = state.mode !== "bypassPermissions";
}

// ------------------------------------------------------------------ //
// Sub-modules (attachments, slash, history) live in chat/*.js. Each   //
// exports an init function that takes shared deps and returns the    //
// public API. No mutual imports between submodules; chat.js is the   //
// only place that knows about all three.                              //
// ------------------------------------------------------------------ //

const attachments = initAttachments({
  els: { input: els.input, attachmentsTray: els.attachmentsTray },
  onError: (msg) => appendErrorBubble(msg),
});

const slash = initSlash({ els: { input: els.input } });

const historyPicker = initHistory({
  els: {
    messages: els.messages,
    historyBtn: els.historyBtn,
    historyPanel: els.historyPanel,
  },
  state,
  appendBubble,
  appendSystemNote,
  sendWS,
  markdown: (typeof marked !== "undefined") ? marked : null,
});

// ------------------------------------------------------------------ //
// Wire up controls                                                    //
// ------------------------------------------------------------------ //

els.composer.addEventListener("submit", (e) => {
  e.preventDefault();
  sendUserMessage();
});
els.input.addEventListener("keydown", (e) => {
  if (slash.isOpen()) {
    if (e.key === "ArrowDown") { e.preventDefault(); slash.move(1); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); slash.move(-1); return; }
    if (e.key === "Escape")   { e.preventDefault(); slash.hide(); return; }
    if (e.key === "Tab")      { e.preventDefault(); slash.insert(); return; }
    if (e.key === "Enter" && !e.shiftKey) {
      // When the popup is open, Enter selects rather than sends.
      e.preventDefault();
      slash.insert();
      return;
    }
  }
  if (e.key === "Enter") {
    // Cmd+Enter, Ctrl+Enter, or plain Enter (without Shift) sends.
    if (e.shiftKey) return;            // Shift+Enter → newline
    e.preventDefault();
    sendUserMessage();
  }
});
els.input.addEventListener("input", () => {
  // Auto-grow up to ~6 lines.
  els.input.style.height = "auto";
  els.input.style.height = Math.min(els.input.scrollHeight, 200) + "px";
  slash.maybeOpen();
});
els.input.addEventListener("blur", () => {
  // Small delay so a click-to-select on the popup still registers
  // before we tear it down.
  setTimeout(() => slash.hide(), 150);
});

// ---- Paste images directly from the clipboard. -----------------
els.input.addEventListener("paste", (e) => {
  const items = e.clipboardData ? e.clipboardData.items : null;
  if (!items) return;
  let handled = false;
  for (const item of items) {
    if (item.kind === "file") {
      const file = item.getAsFile();
      if (file && file.type.startsWith("image/")) {
        handled = true;
        attachments.addImageFile(file);
      }
    }
  }
  if (handled) e.preventDefault();
});

// ---- Drag-and-drop onto the composer. --------------------------
// Two flavors:
//   - File from Finder → text/uri-list contains file:// URIs we can
//     turn into absolute paths.
//   - File from a browser source (e.g. dragging an image out of a
//     web page) → dataTransfer.files only; we can't recover a path,
//     so we either base64 the image or inline the text contents.
function isFileDrag(e) {
  if (!e.dataTransfer) return false;
  const types = e.dataTransfer.types || [];
  return Array.from(types).some(t => t === "Files" || t === "text/uri-list");
}
els.composer.addEventListener("dragenter", (e) => {
  if (isFileDrag(e)) { e.preventDefault(); els.composer.classList.add("drag-over"); }
});
els.composer.addEventListener("dragover", (e) => {
  if (isFileDrag(e)) { e.preventDefault(); els.composer.classList.add("drag-over"); }
});
els.composer.addEventListener("dragleave", () => {
  els.composer.classList.remove("drag-over");
});
els.composer.addEventListener("drop", async (e) => {
  els.composer.classList.remove("drag-over");
  if (!e.dataTransfer) return;
  // Prefer file:// URIs from Finder — they give us absolute paths a
  // browser File object never would.
  const uriList = e.dataTransfer.getData("text/uri-list");
  if (uriList) {
    const paths = uriList.split(/\r?\n/)
      .filter(line => line.startsWith("file://"))
      .map(line => decodeURI(line.replace(/^file:\/\//, "")));
    if (paths.length) {
      e.preventDefault();
      for (const p of paths) attachments.addFilePath(p);
      return;
    }
  }
  if (e.dataTransfer.files && e.dataTransfer.files.length) {
    e.preventDefault();
    for (const file of e.dataTransfer.files) {
      if (file.type.startsWith("image/")) {
        await attachments.addImageFile(file);
      } else {
        await inlineNonImageFile(file);
      }
    }
  }
});

async function inlineNonImageFile(file) {
  if (file.size > 200_000) {
    appendErrorBubble(`${file.name} is too large to inline — drag from Finder for path-based access.`);
    return;
  }
  try {
    const text = await file.text();
    const fenced = `\n\n\`\`\`${(file.name.split('.').pop() || '').toLowerCase()}\n${text}\n\`\`\`\n`;
    els.input.value += fenced;
    els.input.dispatchEvent(new Event("input", { bubbles: true }));
  } catch (_) {
    appendErrorBubble(`Couldn't read ${file.name}.`);
  }
}

// ---- Attach button + hidden file input. ------------------------
if (els.attachBtn && els.attachInput) {
  els.attachBtn.addEventListener("click", () => {
    els.attachInput.click();
  });
  els.attachInput.addEventListener("change", async () => {
    for (const file of els.attachInput.files || []) {
      if (file.type.startsWith("image/")) {
        await attachments.addImageFile(file);
      } else {
        await inlineNonImageFile(file);
      }
    }
    els.attachInput.value = "";  // allow re-selecting the same file
  });
}

function sendUserMessage() {
  const content = els.input.value.trim();
  const imageAttachments = attachments.drain();
  if (!content && !imageAttachments.length) return;
  if (!state.ws || state.ws.readyState !== 1 || !state.ready) {
    appendErrorBubble("Not connected — waiting to reconnect.");
    return;
  }
  const bubble = appendBubble("user");
  // Render image previews into the user bubble too so the transcript
  // shows what we actually sent.
  for (const att of imageAttachments) {
    const img = document.createElement("img");
    img.className = "chat-bubble-image";
    img.src = `data:${att.media_type};base64,${att.data}`;
    bubble.appendChild(img);
  }
  if (content) {
    const textEl = document.createElement("div");
    textEl.textContent = content;
    bubble.appendChild(textEl);
  }
  els.input.value = "";
  els.input.style.height = "auto";
  sendWS({
    type: "user_message",
    content,
    attachments: imageAttachments,
  });
}

els.project.addEventListener("change", () => {
  const path = els.project.value;
  if (!path || path === state.projectPath) return;
  switchToProject(path);
});

function switchToProject(path) {
  if (!path || path === state.projectPath) return;
  state.projectPath = path;
  if (els.project.value !== path) els.project.value = path;
  writeGlobalProject(path);
  // Tell the parent shell (sidebar picker) so it stays in sync when
  // the user switches projects from inside the chat panel itself.
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage(
        { type: "ka-sfskills:project-changed", path },
        location.origin,
      );
    }
  } catch (_) { /* cross-origin parent — should never happen */ }
  appendSystemNote(`Switching to ${path.split("/").pop() || path}…`);
  sendWS({ type: "set_project", path });
}

// Listen for project changes pushed down from the parent shell's
// sidebar picker.
window.addEventListener("message", (e) => {
  if (e.origin && e.origin !== location.origin) return;
  if (!e.data || e.data.type !== "ka-sfskills:project-changed") return;
  switchToProject(e.data.path);
});
els.mode.addEventListener("change", () => {
  const mode = els.mode.value;
  if (mode === state.mode) return;
  state.mode = mode;
  appendSystemNote(`Permission mode → ${mode}`);
  sendWS({ type: "set_permission_mode", mode });
  updateBypassBanner();
});
els.model.addEventListener("change", () => {
  const model = els.model.value;
  if (model === state.model) return;
  state.model = model;
  appendSystemNote(`Model → ${model}`);
  sendWS({ type: "set_model", model });
});
if (els.historyBtn) {
  els.historyBtn.addEventListener("click", () => {
    if (historyPicker.isOpen()) {
      historyPicker.hide();
    } else {
      historyPicker.show();
      historyPicker.refresh();
    }
  });
  // Close on click outside the panel.
  document.addEventListener("click", (e) => {
    if (!historyPicker.isOpen()) return;
    if (els.historyPanel.contains(e.target)) return;
    if (els.historyBtn.contains(e.target)) return;
    historyPicker.hide();
  });
}

els.newBtn.addEventListener("click", () => {
  sendWS({ type: "stop" });
  els.messages.innerHTML = "";
  state.sessionId = null;
  state.cost = 0;
  state.currentAssistant = null;
  state.currentText = "";
  state.ready = false;
  state.reconnect.attempts = 0;
  state.skipResumeOnNextInit = true;  // explicit fresh session
  // Restore the empty-state element so the loading spinner can render
  // again on the next connect.
  if (els.empty) {
    delete els.empty.dataset.cleared;
    els.empty.style.display = "";
  }
  clearReconnectTimer();
  setTimeout(connect, 100);
});

els.messages.addEventListener("scroll", () => {
  const m = els.messages;
  state.autoscroll = m.scrollHeight - m.scrollTop - m.clientHeight < 60;
});

function appendSystemNote(text) {
  clearEmptyState();
  const div = document.createElement("div");
  div.className = "chat-system-note";
  div.textContent = text;
  els.messages.appendChild(div);
  maybeScroll();
}

// ------------------------------------------------------------------ //
// Boot                                                                //
// ------------------------------------------------------------------ //

document.addEventListener("DOMContentLoaded", async () => {
  await loadProjects();
  connect();
});
