# /start-dashboard — launch the dashboard server

Brings up the claude-code-dashboard at `http://localhost:9000` if it
isn't already running. Hand-rolled because Claude Code's plugin
SessionStart hooks don't reliably auto-launch in every host (VS Code,
JetBrains, raw CLI). This is the supported manual entry point.

What you'll see at `http://localhost:9000`:

- **Home** — overview of any installed content plugins, running agents.
- **Agents / Skills / Commands** — content from the active plugin.
- **Live** — real-time event stream + running-agent panel (SSE-driven).
- **Graph** — relationship view across agents and skills.
- **Decision trees** — routing logic agents consult.
- **Chat** — slide-out side panel (every page) or full-page `/chat`.
  A real `claude` CLI subprocess fronted by a WebSocket — your full
  Claude Code context (plugins, MCP servers, slash commands, hooks).
- **Edit source** — every detail page has an in-place CodeMirror editor.
- **Cmd-K** — universal search across every installed plugin.

Requires `aiohttp`, `aiohttp_jinja2`, and `jinja2` on the Python the
plugin runs against (the server prints a clear install hint if any
are missing).

---

## Step 1 — Check if it's already running

```bash
if curl -s --max-time 1 http://127.0.0.1:9000/health > /dev/null 2>&1; then
  echo "ALREADY_RUNNING"
else
  echo "NOT_RUNNING"
fi
```

If output is `ALREADY_RUNNING`, tell the user:

```
✓ Dashboard already running at http://localhost:9000
```

…and STOP. Do not relaunch.

---

## Step 2 — Find the dashboard script

If not running, locate `dashboard_server.py`. The dashboard ships as
its own plugin (`claude-code-dashboard`), so the canonical path is:

```bash
ls -d ~/.claude/plugins/cache/*/claude-code-dashboard/*/scripts/dashboard_server.py 2>/dev/null | sort -V | tail -1
```

Fallback for users still on the pre-0.2 bundled layout (where the
dashboard lived inside ka-sfskills):

```bash
ls -d ~/.claude/plugins/cache/ka-sfskills/ka-sfskills/*/scripts/dashboard_server.py 2>/dev/null | sort -V | tail -1
```

Capture the first non-empty result as `$SCRIPT`. If both are empty:

```
✘ Could not find dashboard_server.py.
  Install: claude plugin install claude-code-dashboard
```

…and STOP.

---

## Step 3 — Launch in the background

```bash
mkdir -p ~/.claude/dashboard
nohup python3 "$SCRIPT" > ~/.claude/dashboard/dashboard.log 2>&1 &
echo $! > ~/.claude/dashboard/dashboard.pid
disown
sleep 1
```

---

## Step 4 — Verify it started

```bash
sleep 1
if curl -s --max-time 1 http://127.0.0.1:9000/health > /dev/null 2>&1; then
  echo "STARTED"
else
  echo "FAILED — see ~/.claude/dashboard/dashboard.log"
fi
```

If `STARTED`, tell the user:

```
✓ Dashboard launched in the background.
  http://localhost:9000
  Logs: ~/.claude/dashboard/dashboard.log
```
