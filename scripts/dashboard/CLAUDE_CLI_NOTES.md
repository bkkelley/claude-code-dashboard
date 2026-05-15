# Claude Code CLI — Spike Notes

Captured during Phase C0 of the chat-and-edit build. The chat panel
subprocess shells out to `claude` with the flags documented here.
Verified against Claude Code v2.1.141 on macOS, 2026-05.

## Headline facts

- Binary lives at `~/.local/bin/claude`. Drives Anthropic's API; auth via
  the user's existing keychain (OAuth) or `ANTHROPIC_API_KEY` env var.
- Headless mode is `-p`/`--print`. Combined with
  `--output-format stream-json` it emits one JSON object per line on stdout.
- Stream input is `--input-format stream-json` — we can keep stdin open
  and write user messages as we go for a long-lived conversation.

## Flags we will use

| Flag | Why |
|---|---|
| `-p` / `--print` | Non-interactive mode. Mandatory for our subprocess driver. |
| `--output-format stream-json` | Emits JSONL events on stdout. Mandatory. |
| `--input-format stream-json` | Lets us push successive user messages over stdin (realtime). |
| `--include-partial-messages` | Yields `stream_event` events with `text_delta` chunks — what powers the typing-effect rendering in the chat panel. Without this we only get full-message events. |
| `--verbose` | Required when both `--print` and `--output-format=stream-json` are set on some claude versions (avoid silent buffering). |
| `--permission-mode <mode>` | Controls how Claude handles tool-use approvals. See modes table below. |
| `--session-id <uuid>` | Tag a session with our own UUID so we can `--resume <id>` later without scraping init events for the id. |
| `--resume <id>` | Continue a prior session. |
| `--no-session-persistence` | Skip writing transcript to disk — useful for "test this agent quickly" workflows. |
| `--fork-session` | When resuming, create a new session ID — for branching conversations. |
| `--add-dir <path>` | Add a directory to the project scope without changing cwd. We'll probably use this when the project picker switches projects without restarting the subprocess. |
| `--mcp-config <file>` | Limit which MCP servers load (default loads everything from the user's config). |
| `--max-budget-usd <amount>` | Spend cap. **See note on cache creation below.** |
| `--model <alias>` | "opus", "sonnet", "haiku". Default is whatever the user's `~/.claude/settings.json` has. |
| `--dangerously-skip-permissions` | Aliased to `--permission-mode bypassPermissions`. Skips all prompts. Surface with a red banner. |

## Permission modes

`--permission-mode` accepts: `default`, `acceptEdits`, `auto`,
`bypassPermissions`, `dontAsk`, `plan`.

For the dashboard's selector we expose four:

| UI label | Flag value | Behavior |
|---|---|---|
| **Prompt for each tool** (default) | `default` | Claude requests approval for tool use; we surface as inline modals in the chat panel. Open question — see "Approvals" below. |
| **Auto-approve edits** | `acceptEdits` | Edit/Write don't prompt; Bash and other risky tools still do. |
| **Plan mode** | `plan` | Claude plans without executing tools. |
| **Bypass all permissions** (with red banner) | `bypassPermissions` | Equivalent to `--dangerously-skip-permissions`. |

`auto` and `dontAsk` are not exposed in v1 — they're niche and the four
above cover the user's stated needs.

## Stream-json event vocabulary (verified)

Events come one JSON object per line. The types we'll handle:

```
system.subtype=hook_started     SessionStart / preToolUse / etc. hook fired
system.subtype=hook_response    hook completed, exit_code + stdout/stderr
system.subtype=init             bootstrap: session_id, tools[], mcp_servers[],
                                plugins[], agents[], skills[], slash_commands[],
                                model, permissionMode, cwd, claude_code_version
system.subtype=status           e.g. {status: "requesting"} while waiting on the model
stream_event                    wraps an Anthropic API SSE event; see below
assistant                       the assembled assistant message at end-of-turn
                                (also emitted earlier in non-partial mode)
result                          terminal event for the run: duration_ms,
                                total_cost_usd, num_turns, is_error, errors[],
                                terminal_reason, modelUsage{...}, permission_denials[]
```

`stream_event` shapes (when `--include-partial-messages` is on):

```
event.type = message_start         {message: {id, model, role, usage}}
event.type = content_block_start   {content_block: {type: text|tool_use|thinking, ...}, index}
event.type = content_block_delta   {delta: {type: text_delta|input_json_delta|thinking_delta, text|partial_json|thinking}, index}
event.type = content_block_stop    {index}
event.type = message_delta         {delta: {stop_reason, stop_sequence}, usage}
```

Rendering rules in the chat panel:

- `text_delta` → append `delta.text` to the current `text` content block bubble.
- `content_block_start` with `content_block.type === "tool_use"` → start a new
  collapsible tool card with `content_block.name`; accumulate
  `input_json_delta.partial_json` into the args view.
- `content_block_start` with `content_block.type === "thinking"` → start a
  collapsible "thinking" section; append `thinking_delta` chunks.
- `assistant` event finalizes the bubble (good place to clear partial state).
- `result` shows the total cost / duration in a footer chip.

## Auth

`claude` uses the user's keychain OAuth by default; falls back to
`ANTHROPIC_API_KEY` if set. **Do not pass `--bare`** — it disables OAuth
and keychain reads, so an unauthenticated subprocess emits
`"error":"authentication_failed"` and bails immediately. Verified this
empirically (first spike attempt failed for exactly this reason).

## Cost surprise: cache creation on first call

A tiny `-p "hello"` prompt on Opus 4.7 1M-context **cost $0.56** because
the first turn writes ~89K cache-creation tokens. `--max-budget-usd 0.05`
fired *after* the response completed — the budget check is post-flight.

Implications for the chat UI:

- **Default to Sonnet** (`--model sonnet`) for the chat panel. Opus is
  ~25× more expensive for cache creation. Users opt into Opus via a
  model dropdown.
- Surface `total_cost_usd` from the `result` event in a chip at the
  bottom of each turn so users see the bill in real time.
- Honor a per-session budget the user can set in the chat header.
- Cache reads are cheap on subsequent turns of the same session — so
  the first message is expensive, the rest is fine. Don't restart the
  subprocess between turns when you don't have to.

## Approvals — still partly open

I didn't witness an approval event in the spike (I used
`--permission-mode bypassPermissions` to avoid prompting). Two possible
shapes Claude might use in non-interactive mode:

1. Emit a structured event (likely under `system.subtype=permission_request`
   or as a `stream_event` with a new event type) and wait on stdin for an
   approval message.
2. Block on stdin with a TUI-style prompt (less likely under
   `--input-format stream-json` but worth verifying).

The C1 build will start with **`bypassPermissions` only** to avoid this
complication on the critical path, then add the prompt-and-approve UX
once we observe the real event shape during a tool-use-bearing call.

## Tools auto-loaded for ka-sfskills's session

The `system.init` event confirms our MCP and plugin context is fully
present in subprocesses launched from the dashboard:

- `tools` includes all of Claude Code's built-ins (Bash, Edit, Read, etc.)
  plus every `mcp__plugin_ka-sfskills_sfskills__*` tool.
- `mcp_servers` shows `plugin:ka-sfskills:sfskills` connected.
- `plugins` lists our own plugin + every other plugin installed (python-development,
  full-stack-orchestration, code-documentation).
- `skills` and `agents` include the cross-plugin set (~80 agents, ~40 skills).
- `slash_commands` is the full installed surface.

So when the user chats in the dashboard side-panel, they get the same
context they'd get in their terminal session — `/find-agent`, `/review`,
`mcp__plugin_ka-sfskills_sfskills__suggest_agent`, all of it works.

## Open items going into C1

1. **Approval event shape** — verify on a `default` permission-mode run
   that triggers a tool. Defer the UI for approvals until then.
2. **Input shape** — verify the JSON object Claude expects on stdin in
   `--input-format stream-json` mode. Likely
   `{"type":"user","message":{"role":"user","content":[{"type":"text","text":"..."}]}}`
   but worth confirming.
3. **Hook events** — `--include-hook-events` adds hook lifecycle events
   to stdout. Likely too noisy for the chat panel; the dashboard's `/live`
   page already tails `events.jsonl` for that. Default off.
4. **Plan-mode UI** — when `permission-mode=plan`, what events arrive?
   May be the same `text_delta` stream with no tool execution. To be
   verified during C4 frontend work.
5. **Cost per model** — record the actual cost-per-1k for sonnet vs opus
   on the first turn so the budget chip shows a realistic forecast.
