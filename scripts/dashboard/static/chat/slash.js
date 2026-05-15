/* ka-sfskills · chat slash-command autocomplete
 *
 * When the user types '/' as the first character of the chat input,
 * we show a floating list of available commands sourced from
 * /api/slash-options. Arrow keys navigate, Enter/Tab inserts, Esc
 * closes. Only opens on a leading slash followed by command-name
 * chars; pasted file paths like /Users/... are left alone.
 */
import { escapeHtml } from "../lib/utils.js";

/**
 * @param {{ els: { input: HTMLTextAreaElement } }} deps
 */
export function initSlash({ els }) {
  /** @type {HTMLElement | null} */
  const popup = document.getElementById("chat-slash-popup");

  /** @type {APISlashOption[] | null} */
  let options = null;
  /** @type {Promise<void> | null} */
  let fetchPromise = null;
  /** @type {APISlashOption[]} */
  let filtered = [];
  let selected = 0;
  let open = false;

  async function ensureLoaded() {
    if (options !== null) return;
    if (!fetchPromise) {
      fetchPromise = fetch("/api/slash-options", { credentials: "same-origin" })
        .then((r) => {
          if (!r.ok) throw new Error(`status ${r.status}`);
          return r.json();
        })
        .then((body) => { options = body.commands || []; })
        .catch(() => {
          // Allow the next keypress to retry instead of permanently
          // serving an empty list after one transient failure.
          fetchPromise = null;
        });
    }
    await fetchPromise;
  }

  function show() {
    if (!popup) return;
    popup.hidden = false;
    open = true;
  }

  function hide() {
    if (!popup) return;
    popup.hidden = true;
    open = false;
  }

  function render() {
    if (!popup) return;
    popup.innerHTML = "";
    if (!filtered.length) {
      const empty = document.createElement("div");
      empty.className = "chat-slash-empty";
      empty.textContent = "No matching commands";
      popup.appendChild(empty);
      return;
    }
    filtered.forEach((cmd, idx) => {
      const row = document.createElement("div");
      row.className = "chat-slash-row" + (idx === selected ? " active" : "");
      row.setAttribute("role", "option");
      row.innerHTML = `
        <span class="chat-slash-name">/${escapeHtml(cmd.name)}</span>
        <span class="chat-slash-desc">${escapeHtml(cmd.description || cmd.title || "")}</span>
      `;
      row.addEventListener("mousedown", (e) => {
        // mousedown (not click) so the textarea doesn't lose focus
        // before insert() runs.
        e.preventDefault();
        selected = idx;
        insert();
      });
      popup.appendChild(row);
    });
  }

  /** @param {string} prefix */
  function update(prefix) {
    const list = options || [];
    const lower = prefix.toLowerCase();
    filtered = list
      .filter((c) => c.name.toLowerCase().startsWith(lower))
      .slice(0, 12);
    selected = 0;
    render();
    if (filtered.length || prefix) show();
    else hide();
  }

  /** @param {number} delta */
  function move(delta) {
    if (!filtered.length) return;
    selected = (selected + delta + filtered.length) % filtered.length;
    render();
  }

  function insert() {
    const cmd = filtered[selected];
    if (!cmd) { hide(); return; }
    // Replace the leading "/foo" token with "/cmd ". We popped open
    // on /^\/[a-zA-Z0-9_-]*$/, so the partial token is the entire
    // textarea value at this point.
    const newToken = `/${cmd.name} `;
    els.input.value = newToken;
    els.input.dispatchEvent(new Event("input", { bubbles: true }));
    const cursor = newToken.length;
    els.input.setSelectionRange(cursor, cursor);
    hide();
  }

  function maybeOpen() {
    const value = els.input.value;
    // Only treat a leading slash as a command prefix if everything
    // after it looks like a command name — kebab-case identifier
    // chars only. This stops the popup from triggering when a file
    // path like "/Users/..." gets injected by the drag-drop handler.
    const m = value.match(/^\/([a-zA-Z0-9_-]*)$/);
    if (!m) { hide(); return; }
    ensureLoaded().then(() => {
      const cur = els.input.value;
      const cm = cur.match(/^\/([a-zA-Z0-9_-]*)$/);
      if (!cm) { hide(); return; }
      update(cm[1]);
    });
  }

  return {
    isOpen: () => open,
    show, hide, move, insert, maybeOpen,
  };
}
