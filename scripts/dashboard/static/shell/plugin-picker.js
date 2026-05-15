/* ka-sfskills · sidebar plugin picker (multi-plugin)
 *
 * Hidden when only one plugin is installed. When two or more
 * content plugins ship a dashboard manifest, this picker scopes the
 * Agents/Skills/Commands pages by which one's active.
 *
 * The Live tab, Cmd-K, and the chat panel stay global — those work
 * across every installed plugin without scoping.
 */
export const pluginPicker = {
  el: null,
  wrapper: null,
  plugins: [],

  async init() {
    this.wrapper = document.getElementById("sidebar-plugin-picker");
    this.el = document.getElementById("global-plugin");
    if (!this.wrapper || !this.el) return;
    try {
      const res = await fetch("/api/dashboard/plugins", { credentials: "same-origin" });
      const body = await res.json();
      this.plugins = body.plugins || [];
    } catch (_) {
      this.plugins = [];
    }
    this.render();
    this.el.addEventListener("change", () => this._onChange());
  },

  render() {
    if (!this.wrapper || !this.el) return;
    // One-plugin (or zero) installs hide the picker entirely. It only
    // becomes useful when the user has multiple dashboard-aware plugins.
    if (this.plugins.length < 2) {
      this.wrapper.hidden = true;
      return;
    }
    this.wrapper.hidden = false;
    this.el.innerHTML = "";
    for (const p of this.plugins) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.title;
      opt.title = p.id;
      if (p.is_active) opt.selected = true;
      this.el.appendChild(opt);
    }
  },

  async _onChange() {
    const id = this.el.value;
    if (!id) return;
    try {
      const res = await fetch("/api/dashboard/active-plugin", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
      if (!res.ok) throw new Error(`status ${res.status}`);
    } catch (err) {
      console.warn("plugin-picker: set active failed", err);
      return;
    }
    // Reload so the content pages re-render scoped to the new plugin.
    location.reload();
  },
};
