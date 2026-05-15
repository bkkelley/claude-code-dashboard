"""Discovery: which Claude Code plugins want to be visible in the dashboard.

Scans ``~/.claude/plugins/cache/<owner>/<plugin>/<version>/`` for plugins
that ship a ``.claude-plugin/dashboard.json`` manifest. Returns the
short-lived index the sidebar plugin picker (and the per-page content
loaders) consult.

A plugin without a dashboard.json manifest is silently skipped — that's
the "I'm not interested in being browsed in the dashboard" signal.

This module also exposes the **active plugin** — what the user's most
recent selection was, stored in ``data_dir() / active-plugin.txt``.
Defaults to "the dashboard's own repo" (i.e. ka-sfskills today) when
no selection has been made, which keeps existing single-plugin
installations behaving exactly as they did pre-discovery.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import paths as paths_mod

log = logging.getLogger(__name__)

CACHE_ROOT = Path.home() / ".claude" / "plugins" / "cache"
ACTIVE_FILE = "active-plugin.txt"


@dataclass(frozen=True)
class PluginEntry:
    """One entry in the plugin picker."""
    id: str               # canonical: "<owner>/<plugin>@<version>"
    title: str
    root: Path            # plugin's install root on disk
    manifest: dict[str, Any]

    @property
    def brand_mark(self) -> str:
        return (self.manifest.get("brand") or {}).get("mark", "?")

    @property
    def brand_color(self) -> str:
        return (self.manifest.get("brand") or {}).get("color", "#999")


def _read_manifest(plugin_root: Path) -> dict[str, Any] | None:
    candidate = plugin_root / ".claude-plugin" / "dashboard.json"
    if not candidate.is_file():
        return None
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("plugins_discovery: %s unreadable (%s)", candidate, exc)
        return None


@lru_cache(maxsize=1)
def list_plugins() -> list[PluginEntry]:
    """Return every installed plugin that ships a dashboard manifest.

    Lru-cached because plugin installs are infrequent and the scan
    touches many directories. The dashboard's existing
    /api/admin/cache-invalidate endpoint busts every cache in
    ``data._INVALIDATABLE_CACHES``; we don't register there because
    the discovery cache is keyed off filesystem state, not in-process
    derivation — call ``list_plugins.cache_clear()`` explicitly if
    you need to re-scan.
    """
    out: list[PluginEntry] = []
    if not CACHE_ROOT.is_dir():
        return out
    # Layout is ~/.claude/plugins/cache/<owner>/<plugin>/<version>/
    for owner in sorted(CACHE_ROOT.iterdir()):
        if not owner.is_dir():
            continue
        for plugin in sorted(owner.iterdir()):
            if not plugin.is_dir():
                continue
            # Take the highest-versioned install (lexical order over
            # iter — semver-aware sort would be nicer but the install
            # cache typically holds one version per plugin).
            versions = sorted(p for p in plugin.iterdir() if p.is_dir())
            if not versions:
                continue
            root = versions[-1]
            manifest = _read_manifest(root)
            if manifest is None:
                continue
            entry_id = f"{owner.name}/{plugin.name}@{root.name}"
            title = manifest.get("title") or plugin.name
            out.append(PluginEntry(
                id=entry_id, title=title, root=root, manifest=manifest,
            ))
    return out


def active_plugin_id() -> str | None:
    """The user's last-chosen plugin id, or None if nothing's pinned.

    Stored in ``data_dir() / active-plugin.txt``. Callers should fall
    back to ``list_plugins()[0]`` when this returns None.
    """
    path = paths_mod.data_dir() / ACTIVE_FILE
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def set_active_plugin(plugin_id: str) -> None:
    path = paths_mod.data_dir() / ACTIVE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plugin_id, encoding="utf-8")


def active_plugin() -> PluginEntry | None:
    """Return the currently-active plugin, or None if no plugins installed."""
    plugins = list_plugins()
    if not plugins:
        return None
    pinned = active_plugin_id()
    if pinned:
        for p in plugins:
            if p.id == pinned:
                return p
    return plugins[0]
