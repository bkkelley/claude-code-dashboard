# claude-code-dashboard

A local web dashboard for any Claude Code plugin. Carved out of
[`ka-sfskills`](https://github.com/bkkelley/ka-sfskills) so other
plugins can adopt the same UI without forking it.

## What you get

- **Chat panel** — a real `claude` CLI subprocess fronted by a
  WebSocket. Same plugins, MCP servers, slash commands, and tools you
  use in your terminal. Side-panel opt-in or full-page.
- **Past sessions** — pick a previous conversation; the transcript
  replays into the chat. Rename / pin / soft-delete entries.
- **Slash autocomplete** — type `/` and a popup lists every command
  exposed by every installed plugin.
- **Attachments** — paste screenshots, drag images from Finder, drop a
  folder to insert its absolute path. Anthropic image content blocks
  go over the wire.
- **Project switcher** — sidebar dropdown. Add via a modal that
  supports text input, drag-from-Finder, the OS native folder picker
  (`osascript` / `zenity` / PowerShell `FolderBrowserDialog`), and a
  server-side directory browser.
- **Live feed** — tail of `events.jsonl`, scoped to subagent + slash
  command lifecycles. Hooks ship with this plugin.
- **Cmd-K** — universal search across every installed plugin's agents,
  skills, commands, decision trees.
- **In-place editor** — CodeMirror editing of agent / skill /
  command markdown. Saves atomically.
- **Multi-plugin** — install several content plugins and switch between
  them in the sidebar.

## Install

```bash
claude plugin install claude-code-dashboard
```

Then start the server (until Claude Code auto-launches plugin
dashboards):

```bash
python3 ~/.claude/plugins/cache/bkkelley/claude-code-dashboard/1.0.0/scripts/dashboard_server.py
```

The dashboard binds to `http://127.0.0.1:9000` by default. Override
the port with `--port`.

On its own, the dashboard is empty — you need at least one *content
plugin* installed. The reference one is
[`ka-sfskills`](https://github.com/bkkelley/ka-sfskills) (Salesforce
agents, skills, commands).

## Make your plugin show up in the dashboard

Add `.claude-plugin/dashboard.json` to your plugin root:

```json
{
  "title": "My Plugin",
  "brand": {
    "mark": "m",
    "color": "#3B82F6"
  },
  "content": {
    "agents":   { "path": "agents",   "pattern": "<id>/AGENT.md" },
    "skills":   { "path": "skills",   "pattern": "<domain>/<name>/SKILL.md" },
    "commands": { "path": "commands", "pattern": "<name>.md" }
  }
}
```

That's the minimum. Optional fields: `status_health` (an MCP tool to
call for the sidebar status pill), `taxonomy.colors` (per-domain color
tokens), `decision_trees`, `templates`. Schema documented in
[docs/dashboard-manifest.md](docs/dashboard-manifest.md).

## Hooks shipped with this plugin

The dashboard ships three generic lifecycle hooks via
`hooks/hooks.json`:

- `track-subagent.py` — emits `subagent_starting` and
  `subagent_completed` events when the user invokes the Task / Agent
  tool. Powers the Live tab's "Running now" panel.
- `track-slash-command.py` — emits `slash_command_invoked` on every
  `/foo` prompt. Powers the slash row in the Live feed.
- `rotate-event-log.py` — rotates `events.jsonl` at boot when it grows
  past a threshold.

If your plugin needs its own lifecycle hooks (post-edit dispatchers,
session-start nudges, etc.) ship them in your own plugin alongside
its `dashboard.json`.

## State

Persisted state lives in `~/.claude/dashboard/`:

```
~/.claude/dashboard/
├── events.jsonl           # the live event stream
├── chat-sessions.json     # last session per project (resume), plus
│                          # rename/pin/delete metadata
├── projects.json          # chat panel's recent projects list
└── active-plugin.txt      # which content plugin the user has pinned
```

Override the root with `KA_DASHBOARD_DATA_DIR`. Test fixtures should
set `paths_mod._data_dir_override = tmp_path`.

If you're migrating from ka-sfskills@0.1 (which stored state in
`~/.claude/ka-sfskills/`), the dashboard will detect the legacy
directory on first launch and offer to move it.

## Development

```bash
git clone https://github.com/bkkelley/claude-code-dashboard.git
cd claude-code-dashboard
pip install aiohttp aiohttp_jinja2 jinja2

# Tests
cd mcp/dashboard-tests && uv run pytest

# Run the dev dashboard
python3 scripts/dashboard_server.py
```

## License

MIT.
