/* ka-sfskills · opt-in chat side panel toggle */

export const chatPanel = {
  el: null,
  frame: null,
  storageKey: "ka-sfskills:chat-panel-open",

  init() {
    this.el = document.getElementById("chat-panel");
    this.frame = document.getElementById("chat-panel-frame");
    if (!this.el || !this.frame) return;

    // If we're hosted inside another window (i.e. the chat panel
    // iframe is rendering its own /chat page, which extends the same
    // base layout), suppress the nested panel entirely. Otherwise
    // localStorage="open" would tell the inner page to open its own
    // panel — which loads another /chat in another iframe — recursing
    // until the browser runs out of room.
    try {
      if (window.top !== window.self) {
        this.el.remove();
        document.querySelectorAll("[data-chat-toggle]").forEach(btn => btn.remove());
        this.el = null;
        return;
      }
    } catch (_) {
      // Cross-origin access on window.top can throw — being unable to
      // read it strongly implies we are framed, so bail out the same way.
      this.el.remove();
      document.querySelectorAll("[data-chat-toggle]").forEach(btn => btn.remove());
      this.el = null;
      return;
    }

    // Toggle button on the topbar.
    document.querySelectorAll("[data-chat-toggle]").forEach(btn => {
      btn.addEventListener("click", () => this.toggle());
    });
    // Close button + Esc.
    document.querySelectorAll("[data-chat-close]").forEach(btn => {
      btn.addEventListener("click", () => this.close());
    });
    document.addEventListener("keydown", (e) => {
      // Cmd-\ / Ctrl-\ toggles panel.
      if ((e.metaKey || e.ctrlKey) && e.key === "\\") {
        e.preventDefault();
        this.toggle();
        return;
      }
      if (e.key === "Escape" && this.isOpen()) {
        // Only close on Esc if the panel itself has focus (or the
        // iframe inside) — don't intercept Esc from other contexts
        // like the Cmd-K modal.
        if (this.isFocused()) {
          this.close();
        }
      }
    });

    // Restore previous open state. Default closed.
    try {
      if (localStorage.getItem(this.storageKey) === "1") this.open();
    } catch (_) { /* private mode etc. — silent */ }
  },

  isOpen() { return this.el && this.el.getAttribute("data-open") === "true"; },

  isFocused() {
    const ae = document.activeElement;
    if (!ae) return false;
    return this.el.contains(ae) || ae === this.frame;
  },

  open() {
    if (!this.el || this.isOpen()) return;
    // Lazy-load the iframe src so we don't pay the chat boot cost
    // until the user actually opens the panel.
    if (!this.frame.src || this.frame.src.endsWith("about:blank")) {
      // ?embed=1 tells the base template to drop the sidebar + topbar
      // so the iframe shows only the chat surface.
      this.frame.src = "/chat?embed=1";
    }
    this.el.hidden = false;
    this.el.setAttribute("aria-hidden", "false");
    // requestAnimationFrame so the .open transition triggers (we
    // need to remove `hidden` before changing transform).
    requestAnimationFrame(() => {
      this.el.setAttribute("data-open", "true");
      document.body.classList.add("chat-panel-open");
    });
    try { localStorage.setItem(this.storageKey, "1"); } catch (_) { /* */ }
  },

  close() {
    if (!this.el || !this.isOpen()) return;
    this.el.setAttribute("data-open", "false");
    document.body.classList.remove("chat-panel-open");
    // Wait for transition to end, then hide for a11y.
    const onEnd = () => {
      if (this.el.getAttribute("data-open") === "false") {
        this.el.hidden = true;
        this.el.setAttribute("aria-hidden", "true");
      }
      this.el.removeEventListener("transitionend", onEnd);
    };
    this.el.addEventListener("transitionend", onEnd);
    try { localStorage.setItem(this.storageKey, "0"); } catch (_) { /* */ }
  },

  toggle() { this.isOpen() ? this.close() : this.open(); },
};
