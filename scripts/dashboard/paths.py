"""Canonical filesystem locations for dashboard state.

The dashboard writes a few JSON / JSONL files into the user's home for
state that has to survive across runs: the events feed, the chat
session store, the chat project list. Historically those lived under
``~/.claude/ka-sfskills/`` because the dashboard *was* ka-sfskills.

As of v0.2, the canonical location is ``~/.claude/dashboard/``. The
``ka-sfskills`` path is honored as a fall-back when no ``dashboard``
directory exists yet — so existing users keep their state until they
explicitly migrate. The override env var ``KA_DASHBOARD_DATA_DIR``
takes precedence over both.

All callers should go through this module rather than constructing
paths directly. Tests monkey-patch ``_data_dir_override`` if they need
to redirect.
"""
from __future__ import annotations

import os
from pathlib import Path

# Test hook — set this to a Path and every accessor below will route
# through it instead of consulting the env / disk. Lets tests pin the
# entire dashboard state directory in one statement.
_data_dir_override: Path | None = None


def data_dir() -> Path:
    """Return the directory dashboard state lives in.

    Precedence:
      1. ``_data_dir_override`` (set by tests via this module)
      2. ``$KA_DASHBOARD_DATA_DIR`` env var
      3. ``~/.claude/dashboard/`` (canonical post-v0.2)
      4. ``~/.claude/ka-sfskills/`` (legacy fall-back when (3) is
         empty/missing — the dashboard prompts the user to migrate
         the first time it sees this)
    """
    if _data_dir_override is not None:
        return _data_dir_override
    env = os.environ.get("KA_DASHBOARD_DATA_DIR")
    if env:
        return Path(env).expanduser()
    canonical = Path.home() / ".claude" / "dashboard"
    if canonical.exists():
        return canonical
    legacy = Path.home() / ".claude" / "ka-sfskills"
    if legacy.exists():
        return legacy
    # First run, no state yet — return the canonical path so callers
    # who mkdir it get the new layout by default.
    return canonical


def events_log_path() -> Path:
    return data_dir() / "events.jsonl"


def sessions_path() -> Path:
    return data_dir() / "chat-sessions.json"


def projects_path() -> Path:
    return data_dir() / "projects.json"


def skill_map_path() -> Path:
    return data_dir() / "skill_map.json"


def legacy_data_dir() -> Path:
    """The pre-v0.2 location; returned so the migration prompt can
    show the user what it's moving."""
    return Path.home() / ".claude" / "ka-sfskills"


# Files the migration helper moves. Each name is relative to
# legacy_data_dir() and lands under data_dir() (the new canonical
# location) with the same filename.
MIGRATION_FILES = (
    "events.jsonl",
    "chat-sessions.json",
    "projects.json",
    "skill_map.json",
)


def has_legacy_state() -> bool:
    """True iff the legacy ka-sfskills state directory exists and
    contains at least one file we'd want to migrate."""
    legacy = legacy_data_dir()
    if not legacy.is_dir():
        return False
    return any((legacy / name).exists() for name in MIGRATION_FILES)


def migrate_from_legacy(dry_run: bool = False) -> dict[str, str]:
    """Copy legacy state into the canonical location.

    Returns a {filename: status} dict where status is one of
    ``"copied"``, ``"skipped: destination exists"``, or
    ``"skipped: no source"``. The original legacy files are left in
    place — the user can ``rm -rf`` them once they've confirmed.

    If ``dry_run`` is True, no files are touched; the same status dict
    is returned with the planned outcome.
    """
    import shutil as _shutil
    legacy = legacy_data_dir()
    dest = Path.home() / ".claude" / "dashboard"
    out: dict[str, str] = {}
    if not legacy.is_dir():
        return out
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    for name in MIGRATION_FILES:
        src = legacy / name
        if not src.exists():
            out[name] = "skipped: no source"
            continue
        target = dest / name
        if target.exists():
            out[name] = "skipped: destination exists"
            continue
        if not dry_run:
            _shutil.copy2(src, target)
        out[name] = "copied"
    return out
