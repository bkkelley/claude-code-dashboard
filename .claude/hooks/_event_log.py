"""Tiny shim hooks use to append events to the dashboard event log.

Standalone copy of the emit logic from sfskills_mcp.events so hooks don't
need the MCP server installed at runtime. Fail-silent; never raises.

Set KA_SFSKILLS_DASHBOARD=0 to disable.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


def _enabled() -> bool:
    return os.environ.get("KA_SFSKILLS_DASHBOARD", "1") != "0"


def _event_log_path() -> Path:
    return Path.home() / ".claude" / "ka-sfskills" / "events.jsonl"


def _session_id() -> str:
    return os.environ.get("CLAUDE_SESSION_ID") or os.environ.get(
        "KA_SFSKILLS_SESSION_ID"
    ) or f"pid-{os.getppid()}"


def emit(event: dict[str, Any]) -> None:
    if not _enabled():
        return
    try:
        event = {
            "id": uuid.uuid4().hex[:12],
            "ts": time.time(),
            "session_id": _session_id(),
            **event,
        }
        path = _event_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception:
        pass


# Rotation: keep the log bounded so it doesn't grow forever. Called from
# the rotate-event-log SessionStart hook — runs once per session, cheap.
ROTATE_MAX_BYTES = 5 * 1024 * 1024   # 5 MB
ROTATE_KEEP_LINES = 2000              # trim to last N lines


def rotate_if_needed() -> bool:
    """Trim the event log to the last ROTATE_KEEP_LINES if it exceeds
    ROTATE_MAX_BYTES. Returns True if a rotation actually happened."""
    try:
        path = _event_log_path()
        if not path.exists():
            return False
        size = path.stat().st_size
        if size <= ROTATE_MAX_BYTES:
            return False
        with path.open("rb") as f:
            data = f.read()
        lines = data.splitlines()
        keep = lines[-ROTATE_KEEP_LINES:]
        with path.open("wb") as f:
            for line in keep:
                f.write(line + b"\n")
        return True
    except Exception:
        return False
