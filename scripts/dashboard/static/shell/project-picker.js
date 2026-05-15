/* ka-sfskills · sidebar project picker */
import { addProjectModal } from "./add-project-modal.js";

export const projectPicker = {
  storageKey: "ka-sfskills:current-project",
  ADD_SENTINEL: "__add__",
  el: null,
  removeBtn: null,
  projects: [],

  async init() {
    this.el = document.getElementById("global-project");
    this.removeBtn = document.getElementById("global-project-remove");
    if (!this.el) return;
    await this.refresh();
    this.el.addEventListener("change", () => this._onChange());
    if (this.removeBtn) {
      this.removeBtn.addEventListener("click", () => this._onRemove());
    }
    // Listen for in-panel changes (postMessage from the chat iframe)
    // so the sidebar mirrors whatever the chat panel set last.
    window.addEventListener("message", (e) => {
      if (e.origin && e.origin !== location.origin) return;
      if (!e.data || e.data.type !== "ka-sfskills:project-changed") return;
      this._writeStorage(e.data.path);
      if (this.el) this.el.value = e.data.path;
    });
  },

  async refresh() {
    try {
      const res = await fetch("/api/chat/projects", { credentials: "same-origin" });
      const body = await res.json();
      this.projects = body.projects || [];
    } catch (_) {
      this.projects = [];
    }
    this.render();
  },

  render() {
    if (!this.el) return;
    this.el.innerHTML = "";
    if (!this.projects.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "(no projects)";
      opt.disabled = true;
      opt.selected = true;
      this.el.appendChild(opt);
    } else {
      const stored = this._readStorage();
      const fallback = this.projects[0].path;
      const chosen = this.projects.some(p => p.path === stored) ? stored : fallback;
      for (const p of this.projects) {
        const opt = document.createElement("option");
        opt.value = p.path;
        opt.textContent = (p.pinned ? "★ " : "") + p.label;
        opt.title = p.path;
        if (p.path === chosen) opt.selected = true;
        this.el.appendChild(opt);
      }
      // Persist the resolved choice so later boots default to it
      // even if storage was empty.
      this._writeStorage(chosen);
    }
    // Always offer an "Add project…" sentinel at the bottom; this
    // is how the user creates a new project entry from the UI.
    const sep = document.createElement("option");
    sep.disabled = true;
    sep.textContent = "─────";
    this.el.appendChild(sep);
    const add = document.createElement("option");
    add.value = this.ADD_SENTINEL;
    add.textContent = "+ Add project…";
    this.el.appendChild(add);
    // Disable the remove button when there's nothing user-added.
    if (this.removeBtn) {
      const current = this._readStorage();
      const entry = this.projects.find(p => p.path === current);
      this.removeBtn.disabled = !entry || entry.pinned;
      this.removeBtn.title = entry && entry.pinned
        ? "Pinned projects can't be removed"
        : "Remove current project from list";
    }
  },

  async _onChange() {
    const value = this.el.value;
    if (value === this.ADD_SENTINEL) {
      // Reset the dropdown so it doesn't sit on the sentinel while
      // the modal is open; the user's previous selection is still
      // what's active.
      this.el.value = this._readStorage();
      addProjectModal.open(async (path) => {
        try {
          const res = await fetch("/api/chat/projects", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path }),
          });
          const body = await res.json().catch(() => ({}));
          if (!res.ok || !body.ok) {
            return body.error || `${res.status} ${res.statusText}`;
          }
          await this.refresh();
          this._setSelected(body.entry?.path || path);
          return null; // success
        } catch (err) {
          return err.message || String(err);
        }
      });
      return;
    }
    this._setSelected(value);
  },

  async _onRemove() {
    const current = this._readStorage();
    if (!current) return;
    const entry = this.projects.find(p => p.path === current);
    if (!entry || entry.pinned) return;
    if (!confirm(`Remove "${entry.label}" from the project list?\n\nThis does not delete anything on disk — you can re-add it by path.`)) return;
    try {
      const res = await fetch(
        `/api/chat/projects?path=${encodeURIComponent(current)}`,
        { method: "DELETE", credentials: "same-origin" },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        alert(`Couldn't remove project: ${body.error || res.statusText}`);
        return;
      }
    } catch (err) {
      alert(`Couldn't remove project: ${err.message || err}`);
      return;
    }
    await this.refresh();
    // Pick whatever's left as the new current — first entry, which is
    // either the pinned default or the next most-recent.
    const next = this.projects[0]?.path || "";
    if (next) this._setSelected(next);
  },

  _setSelected(path) {
    if (!path || path === this.ADD_SENTINEL) return;
    this._writeStorage(path);
    if (this.el && this.el.value !== path) this.el.value = path;
    if (this.removeBtn) {
      const entry = this.projects.find(p => p.path === path);
      this.removeBtn.disabled = !entry || entry.pinned;
    }
    const frame = document.getElementById("chat-panel-frame");
    if (frame && frame.contentWindow) {
      frame.contentWindow.postMessage(
        { type: "ka-sfskills:project-changed", path },
        location.origin,
      );
    }
    document.dispatchEvent(new CustomEvent("ka:project-changed", { detail: { path } }));
  },

  current() { return this._readStorage(); },

  _readStorage() {
    try { return localStorage.getItem(this.storageKey) || ""; }
    catch (_) { return ""; }
  },

  _writeStorage(path) {
    try { localStorage.setItem(this.storageKey, path); }
    catch (_) { /* private mode etc. */ }
  },
};
