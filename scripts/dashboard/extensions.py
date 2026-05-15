"""Loader for plugin extensions — additional dashboard pages contributed
by installed content plugins via dashboard.json.

A plugin's manifest declares:

    "extensions": {
      "python_path":    "dashboard_ext",
      "templates_path": "dashboard_ext/templates",
      "static_path":    "dashboard_ext/static",
      "pages":    [{ id, title, icon, nav_section, route,
                     template, handler_module, handler_fn }],
      "api_routes": [{ method, path, module, fn }]
    }

On startup we walk every discovered plugin, add the python_path to
sys.path, import handler modules, add the templates_path to the
Jinja2 ChoiceLoader, mount static files under
/static/ext/<plugin-id>/, and register routes. Sidebar nav items
come from the merged pages list.

Design notes:

- Plugin code runs in-process. Same trust model as Claude Code hooks
  and MCP servers — the user installs the plugin; the plugin gets to
  run code. We don't sandbox.
- Templates extend _base.html like any other page so the sidebar /
  topbar / chat panel render around them automatically.
- Routing collisions (two plugins both declaring /api/foo) are
  resolved last-write-wins with a warning logged.
"""
from __future__ import annotations

import importlib
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import aiohttp_jinja2
import jinja2
from aiohttp import web

from . import plugins_discovery

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtensionPage:
    """One sidebar-nav entry contributed by an installed plugin."""
    plugin_id: str
    page_id: str
    title: str
    icon: str
    nav_section: str
    route: str
    is_active_check: str  # value put in body[data-active] when this page is shown


def _import_handler(plugin_root: Path, module_name: str, fn_name: str) -> Callable | None:
    """Dynamically import an extension handler.

    Returns None on failure (with a warning logged) — startup
    continues, the missing page just doesn't get wired up.
    """
    try:
        # Importlib reads sys.path; we've already added plugin_root.
        mod = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 — extension bugs shouldn't crash boot
        log.warning(
            "extensions: %s: cannot import module %r (%s)",
            plugin_root.name, module_name, exc,
        )
        return None
    fn = getattr(mod, fn_name, None)
    if fn is None:
        log.warning(
            "extensions: %s: module %r has no %r",
            plugin_root.name, module_name, fn_name,
        )
        return None
    return fn


def _add_templates_path(env: jinja2.Environment, path: Path) -> None:
    """Append a directory to the Jinja2 ChoiceLoader.

    Templates contributed by extensions are looked up *after* the
    dashboard's own templates, so an extension can't accidentally
    shadow `_base.html` or `home.html`.
    """
    existing = env.loader
    new_loader = jinja2.FileSystemLoader(str(path))
    if isinstance(existing, jinja2.ChoiceLoader):
        existing.loaders.append(new_loader)
    elif existing is not None:
        env.loader = jinja2.ChoiceLoader([existing, new_loader])
    else:
        env.loader = new_loader


def collect_extension_pages() -> list[ExtensionPage]:
    """Walk discovered plugins and return their sidebar-nav entries.

    Used by the template context to render the merged sidebar without
    triggering any imports — call before the loader if you need a
    cheap nav-only view.
    """
    out: list[ExtensionPage] = []
    for plugin in plugins_discovery.list_plugins():
        ext = (plugin.manifest.get("extensions") or {})
        for page in (ext.get("pages") or []):
            try:
                out.append(ExtensionPage(
                    plugin_id=plugin.id,
                    page_id=page["id"],
                    title=page.get("title", page["id"]),
                    icon=page.get("icon", "✚"),
                    nav_section=page.get("nav_section", "Workspace"),
                    route=page["route"],
                    is_active_check=f"ext:{plugin.id}:{page['id']}",
                ))
            except KeyError as exc:
                log.warning(
                    "extensions: %s: page entry missing required field %s",
                    plugin.id, exc,
                )
    return out


def mount_all(app: web.Application) -> list[ExtensionPage]:
    """Install every plugin's extension contribution into the running app.

    Call this once during app setup, after the Jinja2 environment is
    set up and after the dashboard's own routes are added. Returns
    the same list ``collect_extension_pages`` would, so the caller
    can stash it in the app for templates to read later.
    """
    pages: list[ExtensionPage] = []
    env = aiohttp_jinja2.get_env(app)
    seen_routes: dict[str, str] = {}  # path -> plugin_id (for collision warnings)

    for plugin in plugins_discovery.list_plugins():
        ext = (plugin.manifest.get("extensions") or {})
        if not ext:
            continue

        root = plugin.root
        py_path = ext.get("python_path")
        if py_path:
            full = str((root / py_path).resolve())
            if full not in sys.path:
                sys.path.insert(0, full)

        templates_path = ext.get("templates_path")
        if templates_path:
            tdir = (root / templates_path).resolve()
            if tdir.is_dir():
                _add_templates_path(env, tdir)

        static_path = ext.get("static_path")
        if static_path:
            sdir = (root / static_path).resolve()
            if sdir.is_dir():
                # Use the plugin name (not the full id) as the URL prefix
                # so /static/ext/ka-sfskills/orgs.js works regardless of
                # which version is installed.
                short_id = plugin.id.split("/")[-1].split("@")[0]
                prefix = f"/static/ext/{short_id}"
                app.router.add_static(prefix, str(sdir), name=f"ext-static-{short_id}")

        for page in (ext.get("pages") or []):
            try:
                page_id = page["id"]
                route = page["route"]
                template = page["template"]
                handler_module = page["handler_module"]
                handler_fn = page["handler_fn"]
            except KeyError as exc:
                log.warning("extensions: %s: page missing %s", plugin.id, exc)
                continue
            fn = _import_handler(root, handler_module, handler_fn)
            if fn is None:
                continue
            # Wrap the handler with @aiohttp_jinja2.template so the page
            # template renders with the standard context. The handler
            # returns the per-page context dict.
            wrapped = aiohttp_jinja2.template(template)(fn)
            if route in seen_routes:
                log.warning(
                    "extensions: %s overrides /%s previously claimed by %s",
                    plugin.id, route, seen_routes[route],
                )
            seen_routes[route] = plugin.id
            app.router.add_get(route, wrapped)
            pages.append(ExtensionPage(
                plugin_id=plugin.id,
                page_id=page_id,
                title=page.get("title", page_id),
                icon=page.get("icon", "✚"),
                nav_section=page.get("nav_section", "Workspace"),
                route=route,
                is_active_check=f"ext:{plugin.id}:{page_id}",
            ))

        for api in (ext.get("api_routes") or []):
            try:
                method = api["method"].upper()
                path = api["path"]
                fn = _import_handler(root, api["module"], api["fn"])
            except KeyError as exc:
                log.warning("extensions: %s: api_route missing %s", plugin.id, exc)
                continue
            if fn is None:
                continue
            if path in seen_routes:
                log.warning(
                    "extensions: %s overrides %s previously claimed by %s",
                    plugin.id, path, seen_routes[path],
                )
            seen_routes[path] = plugin.id
            app.router.add_route(method, path, fn)

        log.info("extensions: mounted %s (%d pages, %d api routes)",
                 plugin.id,
                 len(ext.get("pages") or []),
                 len(ext.get("api_routes") or []))

    app["extension_pages"] = pages
    return pages
