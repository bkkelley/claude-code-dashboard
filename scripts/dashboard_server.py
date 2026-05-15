#!/usr/bin/env python3
"""ka-sfskills studio dashboard — entry point.

This file's path is referenced by ``commands/start-dashboard.md`` and by
external tooling, so it stays at ``scripts/dashboard_server.py``. The
actual implementation lives in ``scripts/dashboard/`` as a small package
(``app.py``, ``data.py``, ``events.py``, ``templates/``, ``static/``).

Before May 2026 this file was a 178-line single-page aiohttp server that
served ``scripts/live.html`` and ``scripts/library.html`` directly. The
studio replaces that with a multi-page UI: Home / Agents / Skills /
Commands / Live / Graph / Decision trees / Explore / Settings, plus a
Cmd-K global search. See ``STUDIO_BUILD_PLAN.md`` for the full design.

To re-enable the legacy single-page server, ``git checkout`` the
pre-2026-05 revision of this file plus the deleted ``scripts/live.html``
and ``scripts/library.html``.

Usage:
    python3 scripts/dashboard_server.py [--host 127.0.0.1] [--port 9000]
"""
from __future__ import annotations

import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from dashboard.app import main

if __name__ == "__main__":
    sys.exit(main())
