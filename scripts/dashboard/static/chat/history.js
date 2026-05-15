/* ka-sfskills · chat history picker
 *
 * Lists prior sessions for the active project. Click a row to switch
 * into that session (server respawns claude with --resume <id> and we
 * replay the prior user/assistant turns into the bubble list). Each
 * row has a ⋯ menu for rename / pin / delete (soft).
 */
import { escapeHtml, relativeTime } from "../lib/utils.js";

/**
 * @param {{
 *   els: {
 *     messages: HTMLElement;
 *     historyBtn: HTMLButtonElement | null;
 *     historyPanel: HTMLElement | null;
 *   };
 *   state: ChatState;
 *   appendBubble: (kind: "user" | "assistant") => HTMLElement;
 *   appendSystemNote: (text: string) => void;
 *   sendWS: (payload: WSClientMessage) => void;
 *   markdown: { parse: (md: string) => string } | null;
 * }} deps
 */
export function initHistory({ els, state, appendBubble, appendSystemNote, sendWS, markdown }) {
  /** @type {APISessionEntry[]} */
  let sessions = [];
  let open = false;

  function isOpen() { return open; }

  function show() {
    if (!els.historyPanel || !els.historyBtn) return;
    els.historyPanel.hidden = false;
    els.historyBtn.setAttribute("aria-expanded", "true");
    open = true;
  }

  function hide() {
    if (!els.historyPanel || !els.historyBtn) return;
    els.historyPanel.hidden = true;
    els.historyBtn.setAttribute("aria-expanded", "false");
    open = false;
  }

  async function refresh() {
    if (!state.projectPath) return;
    try {
      const url = `/api/chat/sessions?project_path=${encodeURIComponent(state.projectPath)}`;
      const res = await fetch(url, { credentials: "same-origin" });
      if (!res.ok) throw new Error(`status ${res.status}`);
      const body = await res.json();
      sessions = body.sessions || [];
    } catch (_) {
      sessions = [];
    }
    render();
  }

  function render() {
    if (!els.historyPanel) return;
    els.historyPanel.innerHTML = "";
    if (!sessions.length) {
      const empty = document.createElement("div");
      empty.className = "chat-history-empty";
      empty.textContent = "No previous sessions for this project.";
      els.historyPanel.appendChild(empty);
      return;
    }
    for (const s of sessions) {
      const row = document.createElement("div");
      row.className = "chat-history-row" + (s.session_id === state.sessionId ? " active" : "");
      row.setAttribute("role", "option");
      const date = new Date((s.last_used || 0) * 1000);
      const rel = relativeTime(date);
      row.innerHTML = `
        <div class="chat-history-label">
          ${s.pinned ? '<span class="chat-history-pin" title="Pinned">★</span>' : ""}
          ${escapeHtml(s.label || "(empty session)")}
        </div>
        <div class="chat-history-meta">${escapeHtml(rel)}</div>
        <button class="chat-history-menu-btn" type="button"
                data-history-menu="${s.session_id}" title="Actions" aria-label="Session actions">⋯</button>
        <div class="chat-history-menu" data-history-menu-for="${s.session_id}" hidden>
          <button type="button" data-action="rename" data-session="${s.session_id}">Rename</button>
          <button type="button" data-action="pin" data-session="${s.session_id}">${s.pinned ? "Unpin" : "Pin"}</button>
          <button type="button" data-action="delete" data-session="${s.session_id}">Delete</button>
        </div>
      `;
      const labelEl = row.querySelector(".chat-history-label");
      const metaEl = row.querySelector(".chat-history-meta");
      if (labelEl) labelEl.addEventListener("click", () => switchTo(s.session_id, s.label));
      if (metaEl) metaEl.addEventListener("click", () => switchTo(s.session_id, s.label));
      const menuBtn = row.querySelector("[data-history-menu]");
      if (menuBtn) {
        menuBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          /** @type {HTMLElement | null} */
          const menu = row.querySelector(".chat-history-menu");
          if (!menu) return;
          if (!els.historyPanel) return;
          els.historyPanel.querySelectorAll(".chat-history-menu").forEach((m) => {
            const el = /** @type {HTMLElement} */ (m);
            if (m !== menu) el.hidden = true;
          });
          menu.hidden = !menu.hidden;
        });
      }
      row.querySelectorAll(".chat-history-menu button").forEach((b) => {
        const btn = /** @type {HTMLButtonElement} */ (b);
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          const action = btn.dataset.action || "";
          const sid = btn.dataset.session || "";
          handleAction(action, sid);
        });
      });
      els.historyPanel.appendChild(row);
    }
  }

  /** @param {string} sessionId @param {string} [label] */
  function switchTo(sessionId, label) {
    if (!sessionId) return;
    // Wipe the visible transcript so the replay payload renders cleanly.
    els.messages.innerHTML = "";
    state.cost = 0;
    state.currentAssistant = null;
    state.currentText = "";
    state.replayedSessionId = sessionId;
    replay(sessionId).then(() => {
      appendSystemNote(`Switched to: ${label || sessionId.slice(0, 8)}`);
    });
    sendWS({ type: "set_session", session_id: sessionId });
    hide();
  }

  /** @param {string} sessionId */
  async function replay(sessionId) {
    if (!state.projectPath) return;
    try {
      const url = `/api/chat/sessions/${encodeURIComponent(sessionId)}/messages?project_path=${encodeURIComponent(state.projectPath)}`;
      const res = await fetch(url, { credentials: "same-origin" });
      if (!res.ok) return;
      const body = await res.json();
      const messages = body.messages || [];
      for (const m of messages) {
        if (m.role === "user") {
          const bubble = appendBubble("user");
          bubble.textContent = m.text;
        } else if (m.role === "assistant") {
          const bubble = appendBubble("assistant");
          const text = document.createElement("div");
          text.className = "chat-bubble-text markdown";
          try { text.innerHTML = markdown ? markdown.parse(m.text) : m.text; }
          catch (_) { text.textContent = m.text; }
          bubble.appendChild(text);
        }
      }
    } catch (_) { /* swallow */ }
  }

  /** @param {string} action @param {string} sessionId */
  async function handleAction(action, sessionId) {
    if (action === "rename") {
      const current = sessions.find((s) => s.session_id === sessionId);
      const next = prompt("Rename session:", current ? current.label : "");
      if (next === null) return;
      await patchMeta(sessionId, { label: next });
    } else if (action === "pin") {
      const current = sessions.find((s) => s.session_id === sessionId);
      await patchMeta(sessionId, { pinned: !(current && current.pinned) });
    } else if (action === "delete") {
      if (!confirm("Hide this session from the history list? The underlying transcript stays on disk.")) return;
      await patchMeta(sessionId, { deleted: true });
    }
    await refresh();
  }

  /** @param {string} sessionId @param {{label?: string; pinned?: boolean; deleted?: boolean}} body */
  async function patchMeta(sessionId, body) {
    try {
      await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/meta`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_path: state.projectPath, ...body }),
      });
    } catch (_) { /* swallow */ }
  }

  return { isOpen, show, hide, refresh, switchTo, replay };
}
