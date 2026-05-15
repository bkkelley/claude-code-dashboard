# claude-code-dashboard

A local web dashboard for any Claude Code plugin. Carved out of
[`ka-sfskills`](https://github.com/bkkelley/ka-sfskills) at v0.2 so
other plugins can adopt the same UI without forking it.

```
http://localhost:9000
```

- **Chat panel** backed by your real `claude` CLI — same plugins, MCP
  servers, slash commands, and hooks you use in your terminal. Side
  panel or full page.
- **Past sessions** — pick a previous conversation; the transcript
  replays. Rename / pin / soft-delete.
- **Slash autocomplete** — type `/` and a popup lists every command
  exposed by every installed plugin.
- **Attachments** — paste screenshots, drag images from Finder, drop
  a folder to insert its absolute path.
- **Project picker** — sidebar dropdown. Add via a modal that supports
  text input, drag-from-Finder, the native OS folder picker
  (`osascript` / `zenity` / PowerShell `FolderBrowserDialog`), and a
  server-side directory browser.
- **Live feed** — tail of `events.jsonl`, scoped to subagent + slash
  command lifecycles. Hooks ship with this plugin.
- **Cmd-K** — universal search across every installed plugin's
  agents, skills, commands, decision trees.
- **In-place editor** — CodeMirror editing of agent / skill / command
  markdown. Atomic saves.
- **Multi-plugin** — install several content plugins and switch
  between them in the sidebar.

## Install

### For team members — one Terminal command (macOS)

If you're going to use this with [ka-sfskills](https://github.com/bkkelley/ka-sfskills)
(the reference content plugin) and you don't need to hack on the
dashboard itself, the easiest path is the team installer in
[`ka-marketplace`](https://github.com/bkkelley/ka-marketplace).
Open Terminal and paste:

```
bash <(curl -fsSL https://raw.githubusercontent.com/bkkelley/ka-marketplace/main/install.sh)
```

That script installs everything you need (Homebrew, Python, Node,
Salesforce CLI, Claude Code, the plugins) and registers the
marketplace. Idempotent — re-running it does nothing harmful.

Then in Claude Code:

```
/start-dashboard       # opens http://localhost:9000
```

### Manual install

**Prerequisites**

- **Claude Code** ([install guide](https://docs.claude.com/claude-code/getting-started))
- **Python 3.10+** with `pip`
- **Git**

**Install the plugin via marketplace**

```bash
claude plugin marketplace add github.com/bkkelley/ka-marketplace
claude plugin install claude-code-dashboard@kelleyaustin
```

Or from this repo as a local marketplace:

```bash
git clone https://github.com/bkkelley/claude-code-dashboard.git
claude plugin marketplace add ~/code/claude-code-dashboard
claude plugin install claude-code-dashboard@claude-code-dashboard
```

**Install Python deps the dashboard needs**

```bash
python3 -m pip install --user aiohttp aiohttp_jinja2 jinja2
```

**Start the dashboard**

```bash
/start-dashboard
```

…inside Claude Code, OR directly:

```bash
python3 ~/.claude/plugins/cache/*/claude-code-dashboard/*/scripts/dashboard_server.py
```

The dashboard binds to `http://127.0.0.1:9000`. Override with `--port`.

## Make your plugin show up in the dashboard

The dashboard is generic — any Claude Code plugin can opt in by
shipping a `.claude-plugin/dashboard.json` file at its repo root.
The minimum:

```json
{ "title": "My Plugin" }
```

A fully-equipped example (what
[ka-sfskills](https://github.com/bkkelley/ka-sfskills) ships):

```json
{
  "$schema": "https://anthropic.com/claude-code/dashboard-manifest.schema.json",
  "title": "ka-sfskills",
  "brand": { "mark": "k", "color": "#DA7756" },
  "content": {
    "agents":         { "path": "agents",                          "pattern": "<id>/AGENT.md" },
    "skills":         { "path": "skills",                          "pattern": "<domain>/<name>/SKILL.md" },
    "commands":       { "path": "commands",                        "pattern": "<name>.md" },
    "decision_trees": { "path": "standards/decision-trees",        "pattern": "<name>.md" },
    "templates":      { "path": "agents/_shared/templates",        "pattern": null }
  },
  "status_health": {
    "mcp_server": "sfskills",
    "mcp_tool": "health"
  },
  "taxonomy": {
    "colors": {
      "apex": "#6366F1", "lwc": "#0EA5E9", "flow": "#D97706"
    }
  }
}
```

All fields except `title` are optional. The dashboard falls back to
sensible defaults for missing fields. See
[`ARCHITECTURE.md`](./ARCHITECTURE.md#contract-how-a-plugin-opts-in)
for the full schema.

When two or more plugins shipping a manifest are installed, the
sidebar grows a **Plugin** picker so the user can switch between
which one's agents/skills/commands appear in the browse pages. The
chat panel, Cmd-K, and Live feed stay global — they work across every
installed plugin without scoping.

## Hooks shipped with this plugin

Three generic lifecycle hooks ship via `hooks/hooks.json`:

| Hook | Event | Purpose |
|---|---|---|
| `track-subagent.py` | PreToolUse + PostToolUse, matcher `Agent\|Task` | Emits `subagent_starting` / `subagent_completed` events. Powers the Live tab's "Running now" panel. |
| `track-slash-command.py` | UserPromptSubmit | Emits `slash_command_invoked` on `/foo` prompts. Powers the slash row in the Live feed. |
| `rotate-event-log.py` | SessionStart | Rotates `events.jsonl` when it grows past a threshold. |

Content plugins ship their own additional hooks alongside their own
`dashboard.json`. Hooks across plugins are merged by Claude Code's
hooks system; they all write to the same `events.jsonl` via the
shared `_event_log.py` emit shim.

## State

Persisted state lives in `~/.claude/dashboard/`:

```
~/.claude/dashboard/
├── events.jsonl           # the live event stream
├── chat-sessions.json     # last session per project (resume), plus
│                          # rename/pin/delete metadata
├── projects.json          # chat panel's recent projects list
├── active-plugin.txt      # which content plugin the user has pinned
└── dashboard.log          # server stderr (when launched detached)
```

Override the root with `KA_DASHBOARD_DATA_DIR`. Test fixtures should
set `paths_mod._data_dir_override = tmp_path`.

### Migrating from ka-sfskills@0.1

If you're upgrading from pre-v0.2 ka-sfskills (which stored dashboard
state in `~/.claude/ka-sfskills/`), the new dashboard detects the
legacy directory on first launch and prints a one-line warning until
you run:

```bash
python3 .../dashboard_server.py --migrate-legacy
```

That copies events.jsonl + chat-sessions.json + projects.json +
skill_map.json into `~/.claude/dashboard/`. The originals stay on
disk — `rm -rf ~/.claude/ka-sfskills/` only when you're confident the
new layout works.

## Development

```bash
git clone https://github.com/bkkelley/claude-code-dashboard.git
cd claude-code-dashboard

# Install Python deps
pip install aiohttp aiohttp_jinja2 jinja2

# Run the dev dashboard against a content plugin you have locally
python3 scripts/dashboard_server.py

# Run the chat tests (28 of them, all green)
cd mcp/dashboard-tests && uv run pytest

# Type-check the frontend
cd scripts/dashboard/static && npx tsc --noEmit -p jsconfig.json
```

The frontend is vanilla JS as ES modules — no build step, no
`node_modules`. Edit a file, hard-reload (⌘⇧R), see it work. See
[`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full layout.

## Related repos

- [`ka-sfskills`](https://github.com/bkkelley/ka-sfskills) — the
  reference content plugin (Salesforce agents, skills, commands, MCP).
- [`ka-marketplace`](https://github.com/bkkelley/ka-marketplace) — the
  Claude Code marketplace + macOS one-shot installer.

## License

MIT.
