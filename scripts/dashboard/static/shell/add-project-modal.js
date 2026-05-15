/* ka-sfskills · Add-project modal */
import { escapeHtml } from "../lib/utils.js";

export const addProjectModal = {
  el: null,
  input: null,
  dropzone: null,
  form: null,
  errorEl: null,
  submitBtn: null,
  onSubmit: null,        // (path) => Promise<error | null>
  _wired: false,
  // Browser state.
  browser: { el: null, list: null, breadcrumb: null, current: null, currentPath: null, homePath: null },

  init() {
    this.el = document.getElementById("add-project-modal");
    if (!this.el || this._wired) return;
    this.input = document.getElementById("add-project-path");
    this.dropzone = document.getElementById("add-project-dropzone");
    this.form = document.getElementById("add-project-form");
    this.errorEl = document.getElementById("add-project-error");
    this.submitBtn = document.getElementById("add-project-submit");
    this.browser.el = document.getElementById("add-project-browser");
    this.browser.list = document.getElementById("fs-browser-list");
    this.browser.breadcrumb = document.getElementById("fs-browser-breadcrumb");
    this.browser.current = document.getElementById("fs-browser-current");
    this._wired = true;

    // Native OS picker — server shells out to osascript/zenity/PowerShell.
    const nativeBtn = document.getElementById("add-project-native-btn");
    if (nativeBtn) {
      nativeBtn.addEventListener("click", async () => {
        this._setError(null);
        const prevLabel = nativeBtn.textContent;
        nativeBtn.disabled = true;
        nativeBtn.textContent = "Waiting on picker…";
        try {
          const res = await fetch("/api/fs/native-picker", {
            method: "POST",
            credentials: "same-origin",
          });
          const body = await res.json().catch(() => ({}));
          if (!res.ok || !body.ok) {
            this._setError(body.error || `${res.status} ${res.statusText}`);
            return;
          }
          if (body.path) {
            this.input.value = body.path;
            this.input.focus();
          }
          // body.path === null means the user cancelled; just no-op.
        } catch (err) {
          this._setError(err.message || String(err));
        } finally {
          nativeBtn.disabled = false;
          nativeBtn.textContent = prevLabel;
        }
      });
    }

    // Browse button toggles the directory picker.
    document.getElementById("add-project-browse-btn").addEventListener("click", () => {
      if (this.browser.el.hidden) {
        this.browser.el.hidden = false;
        // Seed with the typed path (if it's a dir) or fall back to $HOME.
        const seed = this.input.value.trim();
        this._fsLoad(seed || null);
      } else {
        this.browser.el.hidden = true;
      }
    });
    document.getElementById("fs-browser-up").addEventListener("click", () => {
      if (this.browser.currentPath) {
        const idx = this.browser.currentPath.lastIndexOf("/");
        if (idx > 0) this._fsLoad(this.browser.currentPath.slice(0, idx));
        else if (idx === 0) this._fsLoad("/");
      }
    });
    document.getElementById("fs-browser-home").addEventListener("click", () => {
      this._fsLoad(this.browser.homePath || null);
    });
    document.getElementById("fs-browser-use").addEventListener("click", () => {
      if (!this.browser.currentPath) return;
      this.input.value = this.browser.currentPath;
      this.browser.el.hidden = true;
      this._setError(null);
    });

    // Close on backdrop / cancel.
    this.el.querySelectorAll("[data-add-project-close]").forEach(node => {
      node.addEventListener("click", () => this.close());
    });
    document.addEventListener("keydown", (e) => {
      if (!this.isOpen()) return;
      if (e.key === "Escape") { e.preventDefault(); this.close(); }
    });

    // Submit handler.
    this.form.addEventListener("submit", (e) => {
      e.preventDefault();
      this._submit();
    });

    // Drag-and-drop: pull file:// URI from text/uri-list. Finder and
    // most file managers include this when dragging a folder.
    const dz = this.dropzone;
    ["dragenter", "dragover"].forEach(name => {
      dz.addEventListener(name, (e) => {
        e.preventDefault();
        dz.classList.add("drag-over");
      });
    });
    ["dragleave", "drop"].forEach(name => {
      dz.addEventListener(name, () => dz.classList.remove("drag-over"));
    });
    dz.addEventListener("drop", (e) => {
      e.preventDefault();
      if (!e.dataTransfer) return;
      // Browsers vary in which MIME types they expose for a Finder
      // drag. Try the most reliable sources in order. Log everything
      // so it's debuggable when a particular browser misbehaves.
      const types = Array.from(e.dataTransfer.types || []);
      console.debug("add-project drop types:", types);
      const candidates = [
        "text/uri-list",
        "text/x-moz-url",
        "public.file-url",
        "text/plain",
      ];
      let path = null;
      for (const t of candidates) {
        const raw = e.dataTransfer.getData(t) || "";
        if (!raw) continue;
        console.debug("add-project drop", t, "=>", raw.slice(0, 200));
        const first = raw.split(/\r?\n/).find(line => line.startsWith("file://"));
        if (first) {
          path = decodeURI(first.replace(/^file:\/\//, ""));
          break;
        }
        // Some browsers emit a bare path in text/plain.
        if (raw.startsWith("/")) {
          path = decodeURI(raw.split(/\r?\n/)[0]);
          break;
        }
      }
      // Chrome on recent macOS sometimes gives only a FileSystemEntry.
      // For folders we can read .fullPath, but it's just "/<name>" —
      // useful as a hint but not an absolute path.
      if (!path && e.dataTransfer.items && e.dataTransfer.items.length) {
        const first = e.dataTransfer.items[0];
        const entry = first.webkitGetAsEntry && first.webkitGetAsEntry();
        if (entry) {
          console.debug("add-project drop entry:", entry.fullPath, entry.isDirectory);
        }
      }
      if (path) {
        // Strip trailing slash for consistency with how the user
        // would type it.
        this.input.value = path.replace(/\/$/, "");
        this.input.focus();
        this._setError(null);
        return;
      }
      const typeList = types.length ? ` (browser exposed: ${types.join(", ")})` : "";
      this._setError(
        "Couldn't read an absolute path from the drop. Drag the folder from a Finder/Explorer window, " +
        "or paste the path into the text field above." + typeList
      );
    });
  },

  isOpen() { return this.el && !this.el.hidden; },

  open(onSubmit) {
    this.init();
    if (!this.el) return;
    this.onSubmit = onSubmit;
    this.input.value = "";
    this._setError(null);
    // Reset the browser view; user explicitly toggles Browse… to show.
    if (this.browser.el) this.browser.el.hidden = true;
    this.el.hidden = false;
    requestAnimationFrame(() => this.input.focus());
  },

  close() {
    if (!this.el) return;
    this.el.hidden = true;
    this.onSubmit = null;
  },

  async _submit() {
    const path = (this.input.value || "").trim();
    if (!path) {
      this._setError("Enter a path or drop a folder.");
      return;
    }
    if (!this.onSubmit) { this.close(); return; }
    this.submitBtn.disabled = true;
    this._setError(null);
    const err = await this.onSubmit(path);
    this.submitBtn.disabled = false;
    if (err) {
      this._setError(err);
      return;
    }
    this.close();
  },

  _setError(msg) {
    if (!this.errorEl) return;
    if (!msg) {
      this.errorEl.hidden = true;
      this.errorEl.textContent = "";
    } else {
      this.errorEl.hidden = false;
      this.errorEl.textContent = msg;
    }
  },

  async _fsLoad(path) {
    const params = path ? `?path=${encodeURIComponent(path)}` : "";
    this.browser.list.innerHTML = '<div class="fs-browser-empty">Loading…</div>';
    try {
      const res = await fetch(`/api/fs/listdir${params}`, { credentials: "same-origin" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        this.browser.list.innerHTML =
          `<div class="fs-browser-empty">${escapeHtml(body.error || res.statusText)}</div>`;
        return;
      }
      this.browser.currentPath = body.path;
      if (!this.browser.homePath) this.browser.homePath = body.home;
      this.browser.breadcrumb.textContent = body.path;
      this.browser.current.textContent = body.path;
      this.browser.list.innerHTML = "";
      if (!body.entries.length) {
        this.browser.list.innerHTML =
          '<div class="fs-browser-empty">No subdirectories.</div>';
        return;
      }
      for (const entry of body.entries) {
        const row = document.createElement("div");
        row.className = "fs-browser-entry";
        row.innerHTML = `<span class="fs-browser-entry-icon">📁</span>${escapeHtml(entry.name)}`;
        row.title = entry.path;
        row.addEventListener("click", () => this._fsLoad(entry.path));
        this.browser.list.appendChild(row);
      }
    } catch (err) {
      this.browser.list.innerHTML =
        `<div class="fs-browser-empty">${escapeHtml(err.message || String(err))}</div>`;
    }
  },
};
