# claude-code-dashboard · architecture

## Origin

Carved out of [`ka-sfskills`](https://github.com/bkkelley/ka-sfskills)
at v0.2 (May 2026). The dashboard had grown into a generic Claude Code
plugin browser/chat UI; staying coupled to one content plugin was
limiting its reach. Now it's a standalone plugin any content plugin
can adopt by shipping a manifest.

## Layout

```
.
├── .claude-plugin/plugin.json     # Claude Code plugin manifest
├── README.md
├── ARCHITECTURE.md
├── scripts/
│   ├── dashboard_server.py        # CLI entry — argparse + web.run_app
│   └── dashboard/                 # the aiohttp server package
│       ├── app.py                 # routes, middleware, template config
│       ├── chat.py                # chat subprocess manager + WS handler
│       ├── data.py                # content loaders (manifest-aware)
│       ├── edit.py                # in-place editor write paths
│       ├── events.py              # SSE tail of events.jsonl
│       ├── paths.py               # canonical filesystem locations
│       ├── plugins_discovery.py   # scan ~/.claude/plugins/cache/
│       ├── projects.py            # chat-panel recent-projects store
│       ├── templates/             # Jinja2 templates
│       └── static/                # CSS, JS (ES modules), vendored libs
├── commands/
│   └── start-dashboard.md         # /start-dashboard slash command
├── hooks/hooks.json               # plugin-manifest-style hook config
├── .claude/hooks/                 # the actual hook scripts
│   ├── _event_log.py              # shared emit shim
│   ├── track-subagent.py          # PreToolUse/PostToolUse(Agent|Task)
│   ├── track-slash-command.py     # UserPromptSubmit
│   └── rotate-event-log.py        # SessionStart
└── mcp/dashboard-tests/tests/     # 28 chat tests
```

## Contract: how a plugin opts in

A content plugin ships `.claude-plugin/dashboard.json` next to its
`plugin.json`. The minimum:

```json
{ "title": "My Plugin" }
```

A fully-equipped example (what ka-sfskills ships):

```json
{
  "$schema": "https://anthropic.com/claude-code/dashboard-manifest.schema.json",
  "title": "ka-sfskills",
  "brand": { "mark": "k", "color": "#DA7756" },
  "content": {
    "agents":         { "path": "agents",                   "pattern": "<id>/AGENT.md" },
    "skills":         { "path": "skills",                   "pattern": "<domain>/<name>/SKILL.md" },
    "commands":       { "path": "commands",                 "pattern": "<name>.md" },
    "decision_trees": { "path": "standards/decision-trees", "pattern": "<name>.md" },
    "templates":      { "path": "agents/_shared/templates", "pattern": null }
  },
  "status_health": { "mcp_server": "sfskills", "mcp_tool": "health" },
  "taxonomy": { "colors": { "apex": "#6366F1", "lwc": "#0EA5E9" } }
}
```

All fields except `title` are optional. The dashboard falls back to
defaults for missing fields.

## State

```
~/.claude/dashboard/
├── events.jsonl
├── chat-sessions.json
├── projects.json
├── active-plugin.txt
└── dashboard.log
```

Override via `KA_DASHBOARD_DATA_DIR`. Tests inject via
`paths_mod._data_dir_override`.

On first launch the dashboard checks for legacy state at
`~/.claude/ka-sfskills/` and offers to migrate it — protects pre-split
users without auto-mutating their disk.

## Frontend

Vanilla JS as ES modules (no build step), Jinja2 server-rendered
templates, plain CSS with custom-property design tokens. The
non-decision is "no React, no bundler" — the dashboard is local-only,
single-user, and the no-build property is what makes "edit a file,
refresh, see it work" possible.

Module layout under `static/`:

```
static/
├── studio.css                 # @import index over 9 feature files
├── css/{core,sidebar,topbar,chrome,editor,chat,cmdk,modal,misc}.css
├── studio.js                  # entry — wires the shell modules
├── shell/
│   ├── live-feed.js
│   ├── cmdk.js
│   ├── add-project-modal.js
│   ├── project-picker.js
│   ├── plugin-picker.js       # only visible when 2+ content plugins
│   ├── chat-panel.js
│   └── spa.js
├── chat.js                    # iframe + full-page chat entry
├── chat/{attachments,slash,history}.js
├── lib/utils.js
├── types.d.ts                 # WS protocol + API response types
└── jsconfig.json
```

## WebSocket protocol

Documented inline in `scripts/dashboard/chat.py`. Client → server has
seven message kinds (init, user_message, set_project,
set_permission_mode, set_model, set_session, stop). Server → client
emits a `control` channel (ready/error/closed) plus pass-through of
every claude stream-json event (system, stream_event, assistant,
result).

Image attachments travel as Anthropic image content blocks alongside
the text content block, capped at 5 MB raw per image / 14 MB base64
total. WS frame size cap is 16 MB.

## Hooks

Three generic lifecycle hooks ship with the dashboard:

| Hook | Event | Purpose |
|---|---|---|
| `track-subagent.py` | PreToolUse + PostToolUse, matcher `Agent\|Task` | Emits `subagent_starting` / `subagent_completed` |
| `track-slash-command.py` | UserPromptSubmit | Emits `slash_command_invoked` on `/foo` prompts |
| `rotate-event-log.py` | SessionStart | Rotates `events.jsonl` when it grows past a threshold |

Content plugins can ship their own additional hooks alongside their
own `dashboard.json`. Hooks across plugins are merged by Claude Code's
hooks system; they all write to the same `events.jsonl` via the
shared `_event_log.py` emit shim.

## Security

The server binds to `127.0.0.1` by default. An M2 host/origin
middleware rejects any non-loopback `Host` header or non-loopback
`Origin` (when present) — guards against DNS rebinding and against a
browser tab on another origin posting to localhost. WebSocket upgrades
without an `Origin` header are allowed (programmatic clients) but the
threat model assumes a single-user local machine.

All persistent state writes are atomic (`tempfile` + `os.replace`).
