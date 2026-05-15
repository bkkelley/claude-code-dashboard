/**
 * Type definitions for the ka-sfskills dashboard frontend.
 *
 * These types are *ambient* — VS Code / `tsc --checkJs` picks them up
 * via `jsconfig.json`'s `include` glob without any imports in the .js
 * source. Add JSDoc `@type` / `@param` annotations in .js files to
 * opt into checking on a per-function basis.
 *
 * Nothing here ships to the browser; this file is consumed only by
 * the type-checker.
 */

// ---------------------------------------------------------------- //
// WebSocket protocol (server <-> client, scripts/dashboard/chat.py) //
// ---------------------------------------------------------------- //

/** Client-to-server messages over /chat/ws. */
type WSClientMessage =
  | { type: "init"; project_path: string; permission_mode: string; model: string; session_id?: string; resume?: boolean }
  | { type: "user_message"; content: string; attachments?: WSImageAttachment[] }
  | { type: "set_project"; path: string; permission_mode?: string; model?: string }
  | { type: "set_permission_mode"; mode: PermissionMode }
  | { type: "set_model"; model: string }
  | { type: "set_session"; session_id: string }
  | { type: "stop" };

/** Server-to-client envelope. The server forwards stream-json events
 *  from the claude subprocess (system, stream_event, assistant,
 *  result) plus a control channel for ready/error/closed. */
type WSServerMessage =
  | WSControl
  | { type: "system"; subtype: string; [k: string]: any }
  | { type: "stream_event"; event: WSStreamEvent; [k: string]: any }
  | { type: "assistant"; message: { content: WSContentBlock[]; [k: string]: any }; [k: string]: any }
  | { type: "result"; duration_ms?: number; total_cost_usd?: number; is_error?: boolean; [k: string]: any };

type WSControl =
  | { type: "control"; subtype: "ready"; session_id: string; project_path: string; permission_mode: string; model: string; resumed?: boolean; stale_resume_recovered?: boolean }
  | { type: "control"; subtype: "error"; error: string }
  | { type: "control"; subtype: "closed" };

type WSStreamEvent =
  | { type: "content_block_start"; index: number; content_block: { type: string; name?: string; [k: string]: any } }
  | { type: "content_block_delta"; index: number; delta: { type: "text_delta"; text: string } | { type: "thinking_delta"; thinking: string } | { type: "input_json_delta"; partial_json: string } }
  | { type: "message_stop" }
  | { type: "content_block_stop"; index: number };

type WSContentBlock =
  | { type: "text"; text: string }
  | { type: "image"; source: { type: "base64"; media_type: ImageMediaType; data: string } }
  | { type: "tool_use"; id: string; name: string; input: object }
  | { type: "thinking"; thinking?: string };

type WSImageAttachment = {
  kind: "image";
  media_type: ImageMediaType;
  data: string; // base64
};

type ImageMediaType = "image/png" | "image/jpeg" | "image/gif" | "image/webp";

type PermissionMode = "default" | "acceptEdits" | "auto" | "bypassPermissions" | "dontAsk" | "plan";

// ---------------------------------------------------------------- //
// HTTP API response shapes                                          //
// ---------------------------------------------------------------- //

type APIProject = {
  path: string;
  label: string;
  pinned: boolean;
  last_used?: number;
};

type APIProjectsList = { projects: APIProject[] };

type APISessionEntry = {
  session_id: string;
  label: string;
  pinned: boolean;
  last_used: number;
  size_bytes: number;
};

type APISessionsList = { sessions: APISessionEntry[] };

type APISessionMessages = {
  messages: { role: "user" | "assistant"; text: string; ts?: string | number }[];
};

type APISlashOption = { name: string; title: string; description: string };
type APISlashOptions = { commands: APISlashOption[] };

type APIFsListdir =
  | { ok: true; path: string; parent: string | null; home: string; entries: { name: string; path: string }[] }
  | { ok: false; error: string };

type APIFsNativePicker =
  | { ok: true; path: string | null }
  | { ok: false; error: string };

type APIError = { ok: false; error: string };

// ---------------------------------------------------------------- //
// Frontend internal types                                           //
// ---------------------------------------------------------------- //

type PendingAttachment =
  | { kind: "image"; media_type: ImageMediaType; data: string | null; name: string; previewUrl: string | null }
  | { kind: "file-path"; path: string; name: string };

// Vendored CDN globals (marked.min.js, d3.min.js, CodeMirror).
// Loosely typed because we don't depend on their public surface.
declare const marked: {
  setOptions(opts: object): void;
  parse(md: string): string;
};
declare const d3: any;
declare const CodeMirror: any;

type ChatState = {
  ws: WebSocket | null;
  sessionId: string | null;
  projectPath: string | null;
  mode: PermissionMode;
  model: string;
  currentAssistant: HTMLElement | null;
  currentText: string;
  autoscroll: boolean;
  cost: number;
  reconnect: { attempts: number; timer: number | null; stopped: boolean };
  ready: boolean;
  replayedSessionId?: string | null;
  skipResumeOnNextInit?: boolean;
};
