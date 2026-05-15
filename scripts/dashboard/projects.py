"""Project recents store for the chat panel.

Persisted at ``~/.claude/ka-sfskills/projects.json``. Per-user state, not
checked in. Shape:

    {
      "recents": [
        {"path": "/abs/path", "label": "ka-sfskills", "last_used": 1778800000.0},
        ...
      ]
    }

Always surfaces the dashboard's own repo root as a default entry even
when the file is empty — gives a brand-new user something to chat with
on first run.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from . import data
from . import paths as paths_mod

MAX_RECENTS = 50
_SAFE_LABEL = re.compile(r"^[\w. -]{1,80}$")


def __getattr__(name: str):
    """Module-level attribute proxy.

    ``projects.STORE_PATH`` resolves dynamically through ``paths_mod``
    so tests can redirect via ``paths_mod._data_dir_override`` or
    ``KA_DASHBOARD_DATA_DIR`` and every caller sees the new location
    without an import-time snapshot.
    """
    if name == "STORE_PATH":
        return paths_mod.projects_path()
    raise AttributeError(f"module 'projects' has no attribute {name!r}")


# --------------------------------------------------------------------------- #
# Storage                                                                     #
# --------------------------------------------------------------------------- #


def _read_store() -> dict[str, Any]:
    path = paths_mod.projects_path()
    if not path.exists():
        return {"recents": []}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"recents": []}
    if not isinstance(parsed, dict):
        return {"recents": []}
    recents = parsed.get("recents")
    if not isinstance(recents, list):
        parsed["recents"] = []
    return parsed


def _write_store(payload: dict[str, Any]) -> None:
    path = paths_mod.projects_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic tempfile + os.replace, same pattern as edit.py.
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Validation helpers                                                          #
# --------------------------------------------------------------------------- #


def _normalize_path(raw: str) -> Path | None:
    """Return an absolute, real Path or ``None`` if the input is unfit.

    Rejects anything that isn't a directory the user can read. Symlinks
    are resolved so two paths to the same target dedupe.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        candidate = Path(raw).expanduser().resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        return None
    if not candidate.is_dir():
        return None
    return candidate


def _label_for(path: Path, supplied: str | None) -> str:
    if supplied and _SAFE_LABEL.match(supplied):
        return supplied.strip()
    return path.name or str(path)


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


def default_project() -> dict[str, Any]:
    """Repo root the dashboard itself was launched against.

    Returned as a "pinned" entry on top of recents so a first-run user
    always has something to chat with.
    """
    p = data.ROOT
    return {
        "path": str(p),
        "label": p.name or str(p),
        "last_used": None,
        "pinned": True,
    }


def list_projects() -> list[dict[str, Any]]:
    """Return ``[default, *recents]``.

    Default (dashboard repo root) is always first. Recents follow in
    most-recent-first order. The default is deduplicated against the
    recents list — if the user explicitly adds the repo root, the
    explicit entry wins (with its own last_used).
    """
    default = default_project()
    store = _read_store()
    seen = {default["path"]}
    out = [default]
    for entry in sorted(
        store.get("recents", []),
        key=lambda e: -(e.get("last_used") or 0),
    ):
        path = entry.get("path")
        if not path or path in seen:
            continue
        seen.add(path)
        out.append({
            "path": path,
            "label": entry.get("label") or Path(path).name,
            "last_used": entry.get("last_used"),
            "pinned": False,
        })
    return out


def add_project(raw_path: str, label: str | None = None) -> dict[str, Any] | None:
    """Add (or update) a project in recents. Returns the stored entry or
    ``None`` if the input was invalid.
    """
    path = _normalize_path(raw_path)
    if path is None:
        return None
    abs_path = str(path)
    store = _read_store()
    recents = [e for e in store.get("recents", []) if e.get("path") != abs_path]
    entry = {
        "path": abs_path,
        "label": _label_for(path, label),
        "last_used": time.time(),
    }
    recents.insert(0, entry)
    # Cap so a frequently-switching user doesn't grow the file unboundedly.
    store["recents"] = recents[:MAX_RECENTS]
    _write_store(store)
    return entry


def remove_project(raw_path: str) -> bool:
    """Drop a project from recents. Returns ``True`` if a row was removed.

    The dashboard repo root (``default_project``) cannot be removed; it
    surfaces automatically on every ``list_projects`` call.
    """
    path = _normalize_path(raw_path)
    if path is None:
        # Tolerate paths that no longer exist on disk — let users prune
        # stale entries.
        try:
            abs_path = str(Path(raw_path).expanduser().resolve(strict=False))
        except (OSError, ValueError, RuntimeError):
            return False
    else:
        abs_path = str(path)
    store = _read_store()
    before = len(store.get("recents", []))
    store["recents"] = [e for e in store.get("recents", []) if e.get("path") != abs_path]
    if len(store["recents"]) == before:
        return False
    _write_store(store)
    return True


def touch_project(raw_path: str) -> None:
    """Bump ``last_used`` on an existing entry. No-op if not in recents.

    Called whenever a chat session starts against a project — keeps the
    sort order honest without the client having to POST every time.
    """
    path = _normalize_path(raw_path)
    if path is None:
        return
    abs_path = str(path)
    store = _read_store()
    for entry in store.get("recents", []):
        if entry.get("path") == abs_path:
            entry["last_used"] = time.time()
            _write_store(store)
            return
    # Not in recents — add it.
    add_project(raw_path)
