#!/usr/bin/env python3
"""UserPromptSubmit hook: emits slash_command_invoked events.

Fires on every user message; we only emit when the prompt starts with
a slash. The dashboard's Live page renders these the same way it
renders subagent events, so users can see when /build-apex, /audit-router,
or any other ka-sfskills command kicks off.

We deliberately don't emit a 'completed' counterpart. Claude Code's Stop
hook fires per assistant turn, not per command, and a single slash
command can produce multiple turns. The dashboard treats slash command
events as fire-and-forget; the event-feed timeline carries them, and the
running panel ages them out after a short window.

Fail-silent. Always exits 0.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _event_log import emit as _emit_event  # type: ignore[import-not-found]
except Exception:
    def _emit_event(_event):  # type: ignore[misc]
        pass


def _read_stdin() -> dict:
    if sys.stdin.isatty():
        return {}
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def main() -> int:
    payload = _read_stdin()
    if not payload:
        return 0

    # Claude Code passes the user's prompt as `prompt` on UserPromptSubmit.
    prompt = (payload.get("prompt") or "").strip()
    if not prompt.startswith("/"):
        return 0

    # First whitespace-delimited token, stripped of the leading slash.
    # "/build-apex Account custom"  →  "build-apex"
    head = prompt.split(None, 1)[0]
    name = head[1:].strip()
    if not name:
        return 0

    # Arguments tail, capped so we don't dump giant pastes into the log.
    args = ""
    if len(prompt) > len(head):
        args = prompt[len(head):].strip()[:200]

    _emit_event({
        "type": "slash_command_invoked",
        "command": name,
        "args": args,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
