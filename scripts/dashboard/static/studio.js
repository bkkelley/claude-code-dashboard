/* ka-sfskills · studio dashboard — ES module entry.
 *
 * Six features used to live in this file as inline IIFE objects. They
 * moved to ./shell/*.js after the cleanup pass:
 *
 *   shell/live-feed.js          — health pill, running count, recent
 *                                 events feed, SSE stream
 *   shell/cmdk.js               — Cmd-K global search modal
 *   shell/add-project-modal.js  — modal w/ path input, native picker,
 *                                 directory browser, Finder drag
 *   shell/project-picker.js     — sidebar dropdown + localStorage
 *                                 sync with chat-panel iframe
 *   shell/chat-panel.js         — opt-in slide-out chat side panel
 *   shell/spa.js                — pjax-style content swaps so the
 *                                 chat panel survives navigation
 *
 * studio.js itself now just imports them, runs init on DOMContentLoaded,
 * and re-runs the small refresh wiring on `spa:ready` (the event the
 * SPA navigator dispatches after a content swap).
 */
import { populateOrgPill, refreshRunning, loadInitialEvents, connectSse } from "./shell/live-feed.js";
import { cmdk } from "./shell/cmdk.js";
import { addProjectModal } from "./shell/add-project-modal.js";
import { projectPicker } from "./shell/project-picker.js";
import { pluginPicker } from "./shell/plugin-picker.js";
import { chatPanel } from "./shell/chat-panel.js";
import { spa } from "./shell/spa.js";

document.addEventListener("DOMContentLoaded", () => {
  populateOrgPill();
  refreshRunning();
  loadInitialEvents();
  connectSse();
  cmdk.init();
  chatPanel.init();
  addProjectModal.init();
  projectPicker.init();
  pluginPicker.init();
  spa.init();
});

// After an SPA content swap, re-seed any event feeds + running count
// that may have appeared in the new content. SSE itself is parent-scope
// and keeps streaming; this just backfills the recent-events list and
// refreshes the running pill.
document.addEventListener("spa:ready", () => {
  refreshRunning();
  loadInitialEvents();
});
