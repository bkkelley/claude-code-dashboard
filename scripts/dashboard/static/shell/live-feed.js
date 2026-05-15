/* ka-sfskills · live event feed (populates org pill, running count, recent events, SSE stream) */
import { escapeHtml, pad } from "../lib/utils.js";

// ------------------------------------------------------------------ //
// Health probe — populates org pill + sidebar counts on first paint  //
// ------------------------------------------------------------------ //

export async function populateOrgPill() {
  const pill = document.querySelector("[data-org-pill]");
  if (!pill) return;
  try {
    const res = await fetch("/api/health", { credentials: "same-origin" });
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    pill.textContent = data.event_log_exists ? "connected · events live" : "no events yet";
  } catch (err) {
    pill.textContent = "offline";
    pill.style.color = "var(--text-3)";
  }
}

// ------------------------------------------------------------------ //
// Running count — populated from /api/running, updated on SSE        //
// ------------------------------------------------------------------ //

export async function refreshRunning() {
  try {
    const res = await fetch("/api/running", { credentials: "same-origin" });
    if (!res.ok) return;
    const running = await res.json();
    const n = running.length;
    document.querySelectorAll("[data-running-count]").forEach(el => {
      el.textContent = String(n);
    });
    document.querySelectorAll("[data-running-pill]").forEach(el => {
      el.textContent = `${n} running`;
      el.hidden = n === 0;
    });
  } catch (err) {
    // network error is non-fatal — leave previous values in place
  }
}

// ------------------------------------------------------------------ //
// Initial event feed — populate from /api/recent-events on page load. //
// Previously templates/live.html server-rendered the initial 30      //
// events into the same [data-event-feed] container that SSE pushes   //
// into; that caused duplicates as soon as the SSE stream caught up.  //
// Single source of truth (this JS renderer) now seeds and updates.   //
// ------------------------------------------------------------------ //

export async function loadInitialEvents() {
  const feeds = document.querySelectorAll("[data-event-feed]");
  if (!feeds.length) return;
  try {
    const res = await fetch("/api/recent-events?limit=30", { credentials: "same-origin" });
    if (!res.ok) throw new Error(`status ${res.status}`);
    const events = await res.json();
    feeds.forEach(feed => {
      feed.innerHTML = "";
      // Render newest first so SSE-pushed prepends are visually consistent.
      const reversed = Array.isArray(events) ? events.slice().reverse() : [];
      for (const event of reversed) {
        const item = renderEvent(event);
        if (item) feed.appendChild(item);
      }
      if (!feed.children.length) {
        feed.innerHTML = '<div class="empty-state" style="padding: 24px 18px; font-size: 12px; color: var(--text-3);">No recent events. Dispatch an agent in Claude Code to see this populate.</div>';
      }
    });
  } catch (err) {
    // Network/JSON error — leave whatever the server rendered alone.
  }
}

// ------------------------------------------------------------------ //
// SSE event feed                                                     //
// ------------------------------------------------------------------ //

let sse = null;
let sseReconnectMs = 1000;

export function connectSse() {
  if (sse) sse.close();
  try {
    sse = new EventSource("/events");
  } catch (err) {
    return;
  }
  sse.onopen = () => { sseReconnectMs = 1000; };
  sse.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);
      // Any agent dispatch/complete is a signal to refresh the count.
      const t = event.type || "";
      if (t.includes("agent") || t.includes("subagent")) {
        refreshRunning();
      }
      // Push into any feed element on the page.
      document.querySelectorAll("[data-event-feed]").forEach(feed => {
        const item = renderEvent(event);
        if (item) feed.prepend(item);
        // Cap to last 100 items so the DOM doesn't grow forever.
        while (feed.children.length > 100) feed.removeChild(feed.lastChild);
      });
    } catch (err) {
      // ignore malformed
    }
  };
  sse.onerror = () => {
    // Backoff reconnect; EventSource auto-reconnects too, but this
    // adds backoff so we don't hammer a dead server.
    setTimeout(connectSse, Math.min(sseReconnectMs, 30000));
    sseReconnectMs = Math.min(sseReconnectMs * 2, 30000);
  };
}

/**
 * Render a single event-feed row from an events.jsonl line.
 * @param {{ type: string; [k: string]: any }} event
 * @returns {HTMLElement | null}
 */
export function renderEvent(event) {
  const t = event.type || "";
  let kindLabel = "event";
  let kindClass = "tool";
  let text = "";
  if (t === "subagent_starting" || t === "agent_dispatch") {
    kindLabel = "agent"; kindClass = "agent";
    text = (event.agent_id || event.subagent_type || event.name || "?") + " dispatched";
  } else if (t === "subagent_completed" || t === "agent_complete") {
    kindLabel = "agent"; kindClass = "agent";
    text = (event.agent_id || event.subagent_type || event.name || "?") + " completed";
  } else if (t === "skill_accessed" || t === "skill_read") {
    kindLabel = "skill"; kindClass = "skill";
    text = event.skill_id || event.skill_path || "?";
  } else if (t === "mcp_tool_call") {
    kindLabel = "mcp"; kindClass = "tool";
    text = event.tool || event.name || "?";
  } else if (t === "hook_fire") {
    kindLabel = "hook"; kindClass = "hook";
    text = (event.hook || "?") + " fired";
  } else if (t === "slash_command_invoked") {
    kindLabel = "slash"; kindClass = "slash";
    const args = event.args ? ` ${event.args}` : "";
    text = `/${event.command || "?"}${args}`;
  } else {
    kindLabel = t || "event";
    text = JSON.stringify(event).slice(0, 80);
  }
  const div = document.createElement("div");
  div.className = "event " + kindClass;
  const ts = event.ts || event.timestamp || Date.now() / 1000;
  const date = new Date(ts * 1000);
  const tsStr = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  div.innerHTML = `
    <span class="ts">${tsStr}</span>
    <span class="kind-pill ${kindClass}">${kindLabel}</span>
    <span class="what">${escapeHtml(text)}</span>
  `;
  return div;
}
