#!/usr/bin/env python3
"""Pre/Post hook for the agent-spawning tool — emits subagent lifecycle events.

The tool that spawns subagents is called 'Agent' in some Claude Code
versions and 'Task' in others; we accept both so this stays wired
across a rename. The matcher in settings.json is 'Agent|Task'.

- PreToolUse(Agent|Task)  → subagent_starting (with subagent_type + description)
- PostToolUse(Agent|Task) → subagent_completed

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

    tool_name = payload.get("tool_name") or ""
    if tool_name not in ("Agent", "Task"):
        return 0

    event_name = payload.get("hook_event_name") or ""
    tool_input = payload.get("tool_input") or {}
    subagent_type = tool_input.get("subagent_type") or "<unknown>"
    description = tool_input.get("description") or ""

    if event_name == "PreToolUse":
        _emit_event({
            "type": "subagent_starting",
            "subagent_type": subagent_type,
            "description": description[:200],
        })
    elif event_name == "PostToolUse":
        _emit_event({
            "type": "subagent_completed",
            "subagent_type": subagent_type,
            "description": description[:200],
        })

    return 0


if __name__ == "__main__":
    sys.exit(main())
