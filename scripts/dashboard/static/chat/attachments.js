/* ka-sfskills · chat attachments module
 *
 * Each pending attachment is either:
 *   { kind: "image", media_type, data, name, previewUrl }
 *   { kind: "file-path", path, name }   // path injected as text
 *
 * On send, image attachments travel as Anthropic image content blocks;
 * file-path attachments are already pasted into the textarea by the
 * time send fires (we drop them in on drop / picker). The tray UI
 * shows both so the user has a visual cue of what's queued.
 */
import { escapeHtml } from "../lib/utils.js";

/**
 * @param {{
 *   els: { input: HTMLTextAreaElement; attachmentsTray: HTMLElement | null };
 *   onError: (msg: string) => void;
 * }} deps
 */
export function initAttachments({ els, onError }) {
  /** @type {PendingAttachment[]} */
  const items = [];
  const MAX_IMAGE_BYTES = 5 * 1024 * 1024;   // 5 MB raw (~6.7 MB base64)
  const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);

  function render() {
    if (!els.attachmentsTray) return;
    if (!items.length) {
      els.attachmentsTray.hidden = true;
      els.attachmentsTray.innerHTML = "";
      return;
    }
    els.attachmentsTray.hidden = false;
    els.attachmentsTray.innerHTML = "";
    items.forEach((att, idx) => {
      const chip = document.createElement("div");
      chip.className = "chat-attachment-chip";
      if (att.kind === "image") {
        chip.innerHTML = `
          <img class="chat-attachment-thumb" alt="">
          <span class="chat-attachment-name">${escapeHtml(att.name || "image")}</span>
          <button class="chat-attachment-remove" type="button" aria-label="Remove">×</button>
        `;
        /** @type {HTMLImageElement | null} */
        const img = chip.querySelector("img");
        if (img && att.previewUrl) img.src = att.previewUrl;
      } else {
        chip.innerHTML = `
          <span class="chat-attachment-icon">📄</span>
          <span class="chat-attachment-name">${escapeHtml(att.name || "file")}</span>
          <button class="chat-attachment-remove" type="button" aria-label="Remove">×</button>
        `;
      }
      const removeBtn = chip.querySelector(".chat-attachment-remove");
      if (removeBtn) {
        removeBtn.addEventListener("click", () => remove(idx));
      }
      els.attachmentsTray.appendChild(chip);
    });
  }

  /** @param {File | null} file */
  function addImageFile(file) {
    if (!file) return Promise.resolve();
    if (!IMAGE_TYPES.has(file.type)) {
      onError(`Unsupported image type: ${file.type}`);
      return Promise.resolve();
    }
    if (file.size > MAX_IMAGE_BYTES) {
      onError(`Image too large (max ${MAX_IMAGE_BYTES / 1024 / 1024}MB).`);
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => {
        // Result is "data:image/png;base64,...". Strip the prefix.
        const result = String(reader.result || "");
        const comma = result.indexOf(",");
        const data = comma >= 0 ? result.slice(comma + 1) : "";
        if (data) {
          items.push({
            kind: "image",
            media_type: /** @type {ImageMediaType} */ (file.type),
            data,
            name: file.name || "pasted-image",
            previewUrl: result,
          });
          render();
        }
        resolve();
      };
      reader.onerror = () => resolve();
      reader.readAsDataURL(file);
    });
  }

  /** @param {string} path */
  function addFilePath(path) {
    if (!path) return;
    // Inject the path into the textarea instead of attaching as bytes.
    // Claude can then Read it via the filesystem; that's the right
    // pattern for code/text files. We also keep a chip so the user
    // can tell that the path was inserted on their behalf.
    const ta = els.input;
    const insert = (ta.value.endsWith(" ") || ta.value === "") ? path : ` ${path}`;
    ta.value += insert;
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    items.push({
      kind: "file-path",
      path,
      name: path.split("/").pop() || path,
    });
    render();
  }

  /** @param {number} idx */
  function remove(idx) {
    const att = items[idx];
    if (att && att.kind === "image") {
      // Null these explicitly so GC can release the base64 sooner.
      att.data = null;
      att.previewUrl = null;
    }
    items.splice(idx, 1);
    render();
  }

  /** Drain the queue into the wire payload, clearing the tray.
   * @returns {WSImageAttachment[]} */
  function drain() {
    const out = items
      .filter((a) => a.kind === "image" && a.data)
      .map((a) => /** @type {WSImageAttachment} */ ({
        kind: "image",
        media_type: a.kind === "image" ? a.media_type : "image/png",
        data: /** @type {string} */ (a.kind === "image" ? a.data : ""),
      }));
    items.length = 0;
    render();
    return out;
  }

  return { addImageFile, addFilePath, remove, drain, render };
}
