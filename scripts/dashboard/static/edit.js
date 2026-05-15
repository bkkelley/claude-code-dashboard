/* ka-sfskills · in-place editor (Phase E2 + E3)
 *
 * Each detail page (agents / skills / commands / decision trees) wraps its
 * markdown in a host element:
 *
 *   <div data-editor-host data-edit-kind="agent" data-edit-id="apex-builder">
 *     <textarea data-editor-source>{{ markdown }}</textarea>
 *   </div>
 *
 * On DOMContentLoaded we promote the textarea into a CodeMirror instance
 * (read-only by default for syntax-highlighted viewing) and wire the
 * Edit / Save / Cancel buttons:
 *
 *   <button data-editor-edit>   — flip the editor to writable
 *   <button data-editor-save>   — POST the current buffer to /api/edit/...
 *   <button data-editor-cancel> — revert to last-saved content + readOnly
 *
 * A status pill (`<span data-editor-status>`) reflects state changes
 * (saving / saved / error / unsaved-changes warning).
 */
(() => {
  "use strict";

  if (typeof CodeMirror === "undefined") return;

  const ENDPOINT = {
    agent: (id) => `/api/edit/agent/${encodeURIComponent(id)}`,
    skill: (id) => `/api/edit/skill/${encodeURIComponent(id)}`,
    command: (id) => `/api/edit/command/${encodeURIComponent(id)}`,
    "decision-tree": (id) => `/api/edit/decision-tree/${encodeURIComponent(id)}`,
  };

  // One CodeMirror instance per host. Most pages only have one.
  // bootEditors is idempotent — it skips hosts that have already been
  // promoted (CodeMirror replaces the textarea with a wrapper, so the
  // selector won't re-match the same node). SPA navigation dispatches
  // 'spa:ready' after swapping #page-content; we re-run setup so new
  // detail pages get wired without a full reload.
  function bootEditors() {
    document.querySelectorAll("[data-editor-host]").forEach(host => {
      if (host.dataset.editorBooted === "1") return;
      host.dataset.editorBooted = "1";
      setupEditor(host);
    });
  }
  bootEditors();
  document.addEventListener("spa:ready", bootEditors);

  function setupEditor(host) {
    const textarea = host.querySelector("[data-editor-source]");
    if (!textarea) return;
    const kind = host.dataset.editKind;
    const id = host.dataset.editId;
    if (!kind || !id || !ENDPOINT[kind]) return;

    const cm = CodeMirror.fromTextArea(textarea, {
      lineNumbers: true,
      lineWrapping: true,
      readOnly: true,
      mode: { name: "markdown", fencedCodeBlocks: true, highlightFormatting: true },
      theme: "default",
      autoCloseBrackets: true,
      matchBrackets: true,
      extraKeys: {
        // Mod = ⌘ on mac, Ctrl on win/linux.
        "Mod-S": (editor) => { if (state.editing) save(); },
        "Esc": () => { if (state.editing) cancel(); },
        "Mod-F": "findPersistent",
      },
    });
    // Auto-size to content while letting big files scroll inside.
    cm.setSize("100%", "auto");
    // CodeMirror sometimes mis-renders inside an initially-hidden panel.
    // A tiny defer + refresh fixes that without observable flicker.
    setTimeout(() => cm.refresh(), 50);

    const editBtn   = document.querySelector("[data-editor-edit]");
    const saveBtn   = document.querySelector("[data-editor-save]");
    const cancelBtn = document.querySelector("[data-editor-cancel]");
    const statusEl  = document.querySelector("[data-editor-status]");

    const state = {
      original: cm.getValue(),
      editing: false,
      saving: false,
      lastSavedAt: null,
    };

    function setStatus(text, tone) {
      if (!statusEl) return;
      statusEl.textContent = text || "";
      statusEl.dataset.tone = tone || "";
    }

    function setEditing(on) {
      state.editing = on;
      cm.setOption("readOnly", !on);
      host.classList.toggle("editing", on);
      if (editBtn) editBtn.hidden = on;
      if (saveBtn) saveBtn.hidden = !on;
      if (cancelBtn) cancelBtn.hidden = !on;
      if (on) {
        cm.focus();
        // Place cursor at end so users can start editing immediately rather
        // than always seeing it perched at (0,0).
        cm.setCursor(cm.lineCount(), 0);
      }
    }

    function beginEdit() {
      state.original = cm.getValue();
      setEditing(true);
      setStatus("editing", "info");
    }

    function cancel() {
      if (!state.editing) return;
      const dirty = cm.getValue() !== state.original;
      if (dirty && !window.confirm("Discard unsaved changes?")) return;
      cm.setValue(state.original);
      setEditing(false);
      setStatus("");
    }

    async function save() {
      if (state.saving) return;
      const content = cm.getValue();
      if (content === state.original) {
        // No changes — just exit edit mode quietly.
        setEditing(false);
        setStatus("");
        return;
      }
      state.saving = true;
      setStatus("saving…", "info");
      try {
        const res = await fetch(ENDPOINT[kind](id), {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content }),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok || !body.ok) {
          const msg = (body && body.error) || `HTTP ${res.status}`;
          setStatus(`save failed: ${msg}`, "error");
          state.saving = false;
          return;
        }
        // Success — buffer becomes the new "original."
        state.original = content;
        state.lastSavedAt = Date.now();
        state.saving = false;
        setEditing(false);
        setStatus("saved just now", "success");
        if (body.warnings && body.warnings.length) {
          setStatus(`saved (warning: ${body.warnings[0]})`, "warning");
        }
        // Tick the "saved" status to relative time after a moment.
        setTimeout(refreshSavedStatus, 30_000);
      } catch (err) {
        setStatus(`save failed: ${err.message || err}`, "error");
        state.saving = false;
      }
    }

    function refreshSavedStatus() {
      if (!state.lastSavedAt) return;
      const secs = Math.round((Date.now() - state.lastSavedAt) / 1000);
      if (secs < 60) {
        setStatus(`saved ${secs}s ago`, "success");
      } else if (secs < 3600) {
        setStatus(`saved ${Math.floor(secs / 60)}m ago`, "success");
      } else {
        setStatus("saved", "success");
      }
      setTimeout(refreshSavedStatus, 30_000);
    }

    // Wire the buttons.
    if (editBtn) editBtn.addEventListener("click", beginEdit);
    if (saveBtn) saveBtn.addEventListener("click", save);
    if (cancelBtn) cancelBtn.addEventListener("click", cancel);

    // Prompt before navigating away with unsaved changes.
    window.addEventListener("beforeunload", (e) => {
      if (state.editing && cm.getValue() !== state.original) {
        e.preventDefault();
        e.returnValue = "";
      }
    });
  }
})();
