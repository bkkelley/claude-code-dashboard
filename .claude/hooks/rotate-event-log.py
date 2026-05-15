#!/usr/bin/env python3
"""SessionStart hook: rotate the event log if it's grown past the cap.

Cheap — runs once per Claude Code session. Trims to the last 2000 lines
when the file exceeds 5MB. Silent unless a rotation actually happens.

Fail-silent. Always exits 0.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _event_log import _event_log_path, rotate_if_needed  # type: ignore
except Exception:
    sys.exit(0)


def main() -> int:
    try:
        rotated = rotate_if_needed()
        if rotated:
            path = _event_log_path()
            print(f"📊 Rotated {path} (trimmed to last 2000 lines)", flush=True)
    except Exception:
        # Hook contract: always exit 0. A rotation failure (permission
        # error, disk full, malformed event-log file, etc.) must not block
        # SessionStart or surface a non-zero exit to Claude Code.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
