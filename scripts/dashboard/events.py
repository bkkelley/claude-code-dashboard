"""Event-log tail used by the SSE stream and the data layer.

Lifted from the old single-page ``dashboard_server.py`` so the new app can
preserve the same SSE behaviour without changing the on-disk contract.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

from . import paths as paths_mod

POLL_INTERVAL_SECONDS = 0.3


def __getattr__(name: str):
    """Module-level attribute proxy.

    ``events.EVENT_LOG`` and ``events.SKILL_MAP_PATH`` resolve dynamically
    through ``paths_mod`` so tests can redirect via
    ``paths_mod._data_dir_override`` (or ``KA_DASHBOARD_DATA_DIR``) and
    every caller sees the new location without an import-time snapshot.
    """
    if name == "EVENT_LOG":
        return paths_mod.events_log_path()
    if name == "SKILL_MAP_PATH":
        return paths_mod.skill_map_path()
    raise AttributeError(f"module 'events' has no attribute {name!r}")


class EventTail:
    """Reads events.jsonl from the end and yields new events as they arrive.

    Survives log rotation (truncate / replace) by detecting size shrink and
    resetting the read cursor. Yields one dict per line; bad lines are
    silently skipped.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.position = path.stat().st_size if path.exists() else 0

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            if self.path.exists():
                size = self.path.stat().st_size
                if size < self.position:
                    self.position = 0
                if size > self.position:
                    with self.path.open("r", encoding="utf-8") as f:
                        f.seek(self.position)
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                yield json.loads(line)
                            except json.JSONDecodeError:
                                continue
                    self.position = size
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


def read_recent_events(path: Path, limit: int) -> list[dict[str, Any]]:
    """Synchronous: return the last ``limit`` parseable events from the log.

    Used to seed a fresh client that just opened the Live page so it has
    context before the first SSE delta arrives.
    """
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        # Read whole file then take tail. The file is bounded to 5MB / 2000
        # lines by the rotate-event-log hook, so this is fine.
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    for raw in lines[-limit * 2:]:  # over-fetch to allow for bad lines
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out[-limit:]
