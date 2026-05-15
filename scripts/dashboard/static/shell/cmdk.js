/* ka-sfskills · Cmd-K global search modal */
import { escapeHtml } from "../lib/utils.js";

export const cmdk = {
  modal: null,
  input: null,
  body: null,
  results: [],
  selected: 0,
  timer: null,

  init() {
    this.modal = document.getElementById("cmdk");
    this.input = document.getElementById("cmdk-input");
    this.body = document.getElementById("cmdk-body");
    if (!this.modal) return;

    document.addEventListener("keydown", (e) => {
      // ⌘K or Ctrl-K to open
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        this.open();
      }
    });
    this.modal.addEventListener("keydown", (e) => this.onModalKey(e));
    document.querySelectorAll("[data-cmdk-open]").forEach(el => {
      el.addEventListener("click", () => this.open());
    });
    document.querySelectorAll("[data-cmdk-close]").forEach(el => {
      el.addEventListener("click", () => this.close());
    });
    this.input.addEventListener("input", () => this.scheduleSearch());
  },

  open() {
    if (!this.modal) return;
    this.modal.hidden = false;
    this.input.value = "";
    this.body.innerHTML = '<div class="cmdk-empty">Start typing to search.</div>';
    setTimeout(() => this.input.focus(), 0);
  },

  close() {
    if (!this.modal) return;
    this.modal.hidden = true;
  },

  scheduleSearch() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.doSearch(), 120);
  },

  async doSearch() {
    const q = this.input.value.trim();
    if (!q) {
      this.body.innerHTML = '<div class="cmdk-empty">Start typing to search.</div>';
      return;
    }
    try {
      const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
      const data = await res.json();
      this.render(data);
    } catch (err) {
      this.body.innerHTML = '<div class="cmdk-empty">Search error.</div>';
    }
  },

  render(data) {
    this.results = [];
    const sections = [
      ["Agents", "agent", "agents", "/agents/", a => ({ name: a.name + ` <span class="badge">${a.category}</span>`, desc: a.summary, href: "/agents/" + encodeURIComponent(a.id) })],
      ["Skills", "skill", "skills", "/skills/", s => ({ name: s.id + ` <span class="badge">${s.domain}</span>`, desc: s.description, href: "/skills/" + encodeURIComponent((s.id || "").replaceAll("/", "__")) })],
      ["Commands", "cmd", "commands", "/commands/", c => ({ name: "/" + c.name, desc: c.description, href: "/commands/" + encodeURIComponent(c.name) })],
      ["Decision trees", "tree", "decision_trees", "/decision-trees/", t => ({ name: t.title, desc: t.name, href: "/decision-trees/" + encodeURIComponent(t.name) })],
      ["Active runs", "run", "runs", "/live", r => ({ name: r.name + " · running", desc: "started " + (r.started_at || "?"), href: "/live" })],
    ];
    const parts = [];
    let totalShown = 0;
    for (const [label, kind, key, _prefix, mapper] of sections) {
      const items = data[key] || [];
      if (!items.length) continue;
      parts.push(`<div class="cmdk-section-title">${label}<span class="count">${items.length}</span></div>`);
      for (const it of items) {
        const m = mapper(it);
        this.results.push({ href: m.href });
        parts.push(`
          <a class="cmdk-result" href="${m.href}">
            <span class="cmdk-result-avatar ${kind}">${kind[0].toUpperCase()}</span>
            <span class="cmdk-result-info">
              <span class="cmdk-result-name">${m.name}</span>
              <span class="cmdk-result-desc">${escapeHtml(m.desc || "")}</span>
            </span>
          </a>
        `);
        totalShown++;
      }
    }
    if (!totalShown) {
      this.body.innerHTML = '<div class="cmdk-empty">No matches.</div>';
      return;
    }
    this.body.innerHTML = parts.join("");
    this.selected = 0;
    this.applySelection();
  },

  applySelection() {
    const els = this.body.querySelectorAll(".cmdk-result");
    els.forEach((el, i) => el.classList.toggle("selected", i === this.selected));
    const sel = els[this.selected];
    if (sel) sel.scrollIntoView({ block: "nearest" });
  },

  onModalKey(e) {
    if (e.key === "Escape") {
      e.preventDefault();
      this.close();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      if (this.selected < this.results.length - 1) {
        this.selected++;
        this.applySelection();
      }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (this.selected > 0) {
        this.selected--;
        this.applySelection();
      }
    } else if (e.key === "Enter") {
      const r = this.results[this.selected];
      if (r) {
        e.preventDefault();
        window.location.href = r.href;
      }
    } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "l") {
      e.preventDefault();
      window.location.href = "/live";
    }
  },
};
