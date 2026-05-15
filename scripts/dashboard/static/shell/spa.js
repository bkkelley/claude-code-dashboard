/* ka-sfskills · SPA-style nav (pjax) so the chat panel survives nav */

export const spa = {
  inFlight: null,
  loadedScripts: new Set(),

  init() {
    // Seed the loaded-scripts set with everything already on the
    // page so we don't re-execute the same <script src> twice.
    document.querySelectorAll("script[src]").forEach(s => {
      this.loadedScripts.add(this._normalizeSrc(s.src));
    });
    document.addEventListener("click", (e) => this._onClick(e), true);
    window.addEventListener("popstate", () => this._loadUrl(location.pathname + location.search, false));
  },

  _normalizeSrc(src) {
    try {
      const u = new URL(src, location.href);
      return u.pathname + u.search;
    } catch (_) { return src; }
  },

  _onClick(e) {
    if (e.defaultPrevented) return;
    if (e.button !== 0) return;                // left-click only
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest("a");
    if (!a) return;
    const href = a.getAttribute("href");
    if (!href) return;
    if (a.target && a.target !== "_self") return;
    if (a.hasAttribute("download")) return;
    if (a.dataset.spa === "off") return;
    // Same-origin only; skip hash-only links and external URLs.
    let url;
    try { url = new URL(href, location.href); }
    catch (_) { return; }
    if (url.origin !== location.origin) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
    // /chat full-page would conflict with the side-panel iframe;
    // force a real reload there.
    if (url.pathname === "/chat") return;
    // Static asset paths — let the browser handle.
    if (url.pathname.startsWith("/static/") || url.pathname.startsWith("/api/") || url.pathname.startsWith("/events")) return;
    e.preventDefault();
    this._loadUrl(url.pathname + url.search, true);
  },

  async _loadUrl(path, push) {
    if (this.inFlight) this.inFlight.abort();
    const controller = new AbortController();
    this.inFlight = controller;
    // Cheap progress hint — borrow the topbar running-pill area? No,
    // simpler: set a CSS class on body so callers can style if they want.
    document.body.classList.add("spa-loading");
    try {
      const res = await fetch(path, {
        credentials: "same-origin",
        signal: controller.signal,
        headers: { "X-Requested-With": "spa" },
      });
      if (!res.ok) throw new Error(`status ${res.status}`);
      const html = await res.text();
      const doc = new DOMParser().parseFromString(html, "text/html");
      const newContent = doc.getElementById("page-content");
      const here = document.getElementById("page-content");
      if (!newContent || !here) {
        // Server returned a page without our wrapper (or we lost ours).
        // Fall back to a normal navigation so we don't get stuck.
        location.href = path;
        return;
      }
      // Swap content.
      here.innerHTML = newContent.innerHTML;
      // Update document.title.
      if (doc.title) document.title = doc.title;
      // Update body[data-active] from the new body's attribute so
      // sidebar highlight follows.
      const newActive = doc.body.getAttribute("data-active");
      if (newActive !== null) document.body.setAttribute("data-active", newActive);
      // Load any new <script> tags. We re-execute new-to-the-page src
      // scripts (cached set keeps duplicates from running) and always
      // re-run inline scripts found in the swapped content.
      await this._runScripts(here, doc);
      // Notify per-page modules so they can re-init bindings against
      // the new DOM.
      document.dispatchEvent(new CustomEvent("spa:ready", {
        detail: { path },
      }));
      if (push) history.pushState({ spa: true, path }, "", path);
      window.scrollTo({ top: 0, behavior: "instant" });
    } catch (err) {
      if (err.name !== "AbortError") {
        // On any failure, fall back to a real navigation.
        location.href = path;
      }
    } finally {
      document.body.classList.remove("spa-loading");
      if (this.inFlight === controller) this.inFlight = null;
    }
  },

  async _runScripts(scope, sourceDoc) {
    // Run inline scripts that appear inside the swapped content.
    for (const s of scope.querySelectorAll("script")) {
      if (s.src) continue;                  // covered by sourceDoc walk
      const next = document.createElement("script");
      next.text = s.text;
      s.parentNode.replaceChild(next, s);
    }
    // Pull any new src-scripts from the source page's <head> + #page-content
    // (covers {% block extra_scripts %} which lives in <head>).
    const allSources = [
      ...sourceDoc.head.querySelectorAll("script[src]"),
      ...sourceDoc.querySelectorAll("#page-content script[src]"),
    ];
    for (const s of allSources) {
      const norm = this._normalizeSrc(s.src);
      if (this.loadedScripts.has(norm)) continue;
      this.loadedScripts.add(norm);
      await new Promise((resolve) => {
        const next = document.createElement("script");
        next.src = s.src;
        next.async = false;
        next.onload = next.onerror = resolve;
        document.head.appendChild(next);
      });
    }
    // Also pull any new stylesheet links — codemirror.css etc.
    for (const l of sourceDoc.head.querySelectorAll("link[rel='stylesheet']")) {
      const norm = this._normalizeSrc(l.href);
      const existing = document.head.querySelectorAll("link[rel='stylesheet']");
      const already = Array.from(existing).some(x => this._normalizeSrc(x.href) === norm);
      if (already) continue;
      const next = document.createElement("link");
      next.rel = "stylesheet";
      next.href = l.href;
      document.head.appendChild(next);
    }
  },
};
