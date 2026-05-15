"""aiohttp application factory for the studio dashboard.

The app is a thin shell over ``data.py`` (which does the real work of
reading agents / skills / commands / events). Routes split into two
groups:

- HTML pages — server-rendered with Jinja2. Each page extends ``_base.html``.
- JSON APIs at ``/api/*`` — consumed by client-side JS (Cmd-K, live updates,
  filter changes).

Middleware enforces a Host + Origin guard (closes M2 from
IMPROVEMENT_FOLLOWUPS.md): without this, a malicious local browser tab
could trigger subprocess spawns on ``/api/refresh-graph`` or read the
local event stream via DNS rebinding.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

try:
    from aiohttp import web
except ImportError:
    print(
        "ERROR: aiohttp not installed. Run: pip install aiohttp aiohttp_jinja2 jinja2",
        flush=True,
    )
    raise SystemExit(2)

try:
    import aiohttp_jinja2
    import jinja2
except ImportError:
    print(
        "ERROR: aiohttp_jinja2 / jinja2 not installed. Run: pip install aiohttp_jinja2 jinja2",
        flush=True,
    )
    raise SystemExit(2)

from . import chat as chat_mod
from . import data, edit, events, projects

HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"


# --------------------------------------------------------------------------- #
# Middleware                                                                  #
# --------------------------------------------------------------------------- #

_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _allowed_host(host: str) -> bool:
    """Check Host header against the loopback allowlist (closes M2).

    Accepts ``host`` / ``host:port`` / ``[ipv6]:port`` forms. Reject
    anything else — even on a localhost-bound server, the Host header can
    be set to an arbitrary name in a DNS-rebinding attack, and we use it
    as a CSRF signal.
    """
    if not host:
        return False
    if host.startswith("["):
        # Bracketed IPv6: ``[::1]:9000`` or ``[::1]``.
        end = host.find("]")
        if end < 1:
            return False
        hostname = host[1:end].lower()
    else:
        hostname = host.split(":", 1)[0].lower()
    return hostname in _ALLOWED_HOSTS


@web.middleware
async def host_origin_guard(request: web.Request, handler):
    host = request.headers.get("Host", "")
    if not _allowed_host(host):
        logging.warning("dashboard: rejected host=%r path=%s", host, request.path)
        return web.json_response(
            {"error": "host not allowed", "host": host},
            status=403,
        )
    origin = request.headers.get("Origin")
    if origin:
        # Origin is ``scheme://host[:port]``. Strip the scheme and any path,
        # then hand the remainder to ``_allowed_host`` so bracketed IPv6
        # like ``http://[::1]:9000`` parses the same way the Host header
        # path does. The previous ``split(":", 1)[0].strip("[]")`` form
        # incorrectly rejected bracketed IPv6 because it split on the first
        # colon inside the brackets — caught by test-automator.
        try:
            host_part = origin.split("://", 1)[1].split("/", 1)[0]
        except IndexError:
            host_part = ""
        if not host_part or not _allowed_host(host_part):
            logging.warning(
                "dashboard: rejected origin=%r path=%s", origin, request.path
            )
            return web.json_response(
                {"error": "origin not allowed", "origin": origin},
                status=403,
            )
    return await handler(request)


# --------------------------------------------------------------------------- #
# HTML page handlers                                                          #
# --------------------------------------------------------------------------- #


def _shared_context(active: str) -> dict[str, Any]:
    """Context every page needs: counts for the sidebar, active page marker,
    the active plugin's branding, and any plugin-contributed nav entries
    that the sidebar template should render."""
    m = data.manifest()
    # Collect extension pages without importing the heavy loader here —
    # collect_extension_pages just walks manifests, no imports.
    from . import extensions
    ext_pages = extensions.collect_extension_pages()
    nav_extensions: dict[str, list[dict[str, Any]]] = {}
    for p in ext_pages:
        nav_extensions.setdefault(p.nav_section, []).append({
            "id": p.is_active_check,
            "title": p.title,
            "icon": p.icon,
            "route": p.route,
        })
    return {
        "active": active,
        "agent_count": data.runtime_agent_count(),
        "skill_count": data.skill_count(),
        "command_count": data.command_count(),
        "brand_title": m.get("title", "ka-sfskills"),
        "brand_mark": (m.get("brand") or {}).get("mark", "k"),
        "brand_color": (m.get("brand") or {}).get("color", "#DA7756"),
        "nav_extensions": nav_extensions,
    }


@aiohttp_jinja2.template("home.html")
async def page_home(request: web.Request) -> dict[str, Any]:
    return {
        **_shared_context("home"),
        "running": data.running_agents(),
        "top_skills": data.top_skills(window_seconds=3600, limit=8),
        "recent_agents": data.recent_completed_agents(limit=3),
    }


@aiohttp_jinja2.template("agents/list.html")
async def page_agents(request: web.Request) -> dict[str, Any]:
    category = request.query.get("category") or "all"
    sort = request.query.get("sort", "name")
    agents = data.list_agents(category=category, sort=sort)
    return {
        **_shared_context("agents"),
        "agents": agents,
        "selected_category": category,
        "categories": data.agent_categories(),
    }


@aiohttp_jinja2.template("agents/detail.html")
async def page_agent_detail(request: web.Request) -> dict[str, Any]:
    agent_id = request.match_info["id"]
    agent = data.get_agent(agent_id)
    if agent is None:
        raise web.HTTPNotFound(text=f"Agent {agent_id!r} not found")
    return {**_shared_context("agents"), "agent": agent}


@aiohttp_jinja2.template("skills/list.html")
async def page_skills(request: web.Request) -> dict[str, Any]:
    domain = request.query.get("domain") or "all"
    q = (request.query.get("q") or "").strip()
    skills, total = data.list_skills(domain=domain, q=q, limit=120)
    return {
        **_shared_context("skills"),
        "skills": skills,
        "total": total,
        "selected_domain": domain,
        "search": q,
        "domains": data.skill_domains(),
    }


@aiohttp_jinja2.template("skills/detail.html")
async def page_skill_detail(request: web.Request) -> dict[str, Any]:
    skill_id = request.match_info["id"].replace("__", "/")
    skill = data.get_skill(skill_id)
    if skill is None:
        raise web.HTTPNotFound(text=f"Skill {skill_id!r} not found")
    return {**_shared_context("skills"), "skill": skill}


@aiohttp_jinja2.template("commands/list.html")
async def page_commands(request: web.Request) -> dict[str, Any]:
    return {**_shared_context("commands"), "commands": data.list_commands()}


@aiohttp_jinja2.template("commands/detail.html")
async def page_command_detail(request: web.Request) -> dict[str, Any]:
    name = request.match_info["name"]
    command = data.get_command(name)
    if command is None:
        raise web.HTTPNotFound(text=f"Command {name!r} not found")
    return {**_shared_context("commands"), "command": command}


@aiohttp_jinja2.template("live.html")
async def page_live(request: web.Request) -> dict[str, Any]:
    return {
        **_shared_context("live"),
        "stats": data.live_stats(),
        "running": data.running_agents(),
        "recent_events": data.recent_events(limit=30),
    }


@aiohttp_jinja2.template("graph.html")
async def page_graph(request: web.Request) -> dict[str, Any]:
    return {**_shared_context("graph")}


@aiohttp_jinja2.template("decision_trees/list.html")
async def page_decision_trees(request: web.Request) -> dict[str, Any]:
    return {
        **_shared_context("decision_trees"),
        "trees": data.list_decision_trees(),
    }


@aiohttp_jinja2.template("decision_trees/detail.html")
async def page_decision_tree_detail(request: web.Request) -> dict[str, Any]:
    name = request.match_info["name"]
    tree = data.get_decision_tree(name)
    if tree is None:
        raise web.HTTPNotFound(text=f"Decision tree {name!r} not found")
    return {**_shared_context("decision_trees"), "tree": tree}


@aiohttp_jinja2.template("chat.html")
async def page_chat(request: web.Request) -> dict[str, Any]:
    # ?embed=1 means we're rendering inside the side-panel iframe; the
    # base template uses this to hide the global sidebar + topbar so the
    # iframe shows only the chat UI.
    embed = request.query.get("embed") == "1"
    return {**_shared_context("chat"), "embed": embed}


@aiohttp_jinja2.template("explore.html")
async def page_explore(request: web.Request) -> dict[str, Any]:
    return {**_shared_context("explore")}


@aiohttp_jinja2.template("settings.html")
async def page_settings(request: web.Request) -> dict[str, Any]:
    return {**_shared_context("settings"), "settings": data.settings_snapshot()}


# --------------------------------------------------------------------------- #
# JSON API handlers                                                           #
# --------------------------------------------------------------------------- #


async def api_health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "agent_count": data.runtime_agent_count(),
            "skill_count": data.skill_count(),
            "command_count": data.command_count(),
            "event_log": str(events.EVENT_LOG),
            "event_log_exists": events.EVENT_LOG.exists(),
        }
    )


async def api_search(request: web.Request) -> web.Response:
    q = (request.query.get("q") or "").strip()
    if not q:
        return web.json_response({"agents": [], "skills": [], "commands": [], "decision_trees": [], "runs": []})
    results = data.universal_search(q, limit_per_kind=6)
    return web.json_response(results)


async def api_running(request: web.Request) -> web.Response:
    return web.json_response(data.running_agents())


async def api_recent_events(request: web.Request) -> web.Response:
    try:
        limit = max(1, min(int(request.query.get("limit", "30")), 200))
    except ValueError:
        limit = 30
    return web.json_response(data.recent_events(limit=limit))


async def api_graph(request: web.Request) -> web.Response:
    payload = data.get_graph(rebuild=request.query.get("refresh") == "1")
    if "error" in payload:
        return web.json_response(payload, status=500)
    return web.json_response(payload)


async def api_slash_options(request: web.Request) -> web.Response:
    """Slash-command autocomplete payload for the chat textarea.

    Returns the same commands the /commands page surfaces — name, title,
    one-line description — pre-sorted alphabetically so the client can
    just prefix-filter without re-sorting.
    """
    options = [
        {"name": c["name"], "title": c.get("title", c["name"]), "description": c.get("description", "")}
        for c in data.list_commands()
    ]
    options.sort(key=lambda o: o["name"])
    return web.json_response({"commands": options})


async def _read_edit_body(request: web.Request) -> tuple[str | None, web.Response | None]:
    """Pull ``content`` from a JSON POST body. Returns ``(content, None)`` on
    success or ``(None, response)`` with a 400 on malformed input."""
    try:
        payload = await request.json()
    except (ValueError, web.HTTPException):
        return None, web.json_response(
            {"ok": False, "error": "request body must be valid JSON"},
            status=400,
        )
    if not isinstance(payload, dict) or "content" not in payload:
        return None, web.json_response(
            {"ok": False, "error": "JSON body must include a 'content' field"},
            status=400,
        )
    content = payload.get("content")
    if not isinstance(content, str):
        return None, web.json_response(
            {"ok": False, "error": "'content' must be a string"},
            status=400,
        )
    return content, None


async def api_edit_agent(request: web.Request) -> web.Response:
    agent_id = request.match_info["id"]
    content, err_resp = await _read_edit_body(request)
    if err_resp is not None:
        return err_resp
    body, status = edit.result_to_response(edit.write_agent(agent_id, content or ""))
    return web.json_response(body, status=status)


async def api_edit_skill(request: web.Request) -> web.Response:
    skill_id = request.match_info["id"].replace("__", "/")
    content, err_resp = await _read_edit_body(request)
    if err_resp is not None:
        return err_resp
    body, status = edit.result_to_response(edit.write_skill(skill_id, content or ""))
    return web.json_response(body, status=status)


async def api_edit_command(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    content, err_resp = await _read_edit_body(request)
    if err_resp is not None:
        return err_resp
    body, status = edit.result_to_response(edit.write_command(name, content or ""))
    return web.json_response(body, status=status)


async def api_edit_decision_tree(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    content, err_resp = await _read_edit_body(request)
    if err_resp is not None:
        return err_resp
    body, status = edit.result_to_response(edit.write_decision_tree(name, content or ""))
    return web.json_response(body, status=status)


async def api_dashboard_plugins(request: web.Request) -> web.Response:
    """List installed Claude Code plugins that ship a dashboard manifest."""
    from . import plugins_discovery
    entries = plugins_discovery.list_plugins()
    active = plugins_discovery.active_plugin()
    return web.json_response({
        "plugins": [
            {
                "id": p.id,
                "title": p.title,
                "brand_mark": p.brand_mark,
                "brand_color": p.brand_color,
                "is_active": active is not None and active.id == p.id,
            }
            for p in entries
        ],
        "active_id": active.id if active else None,
    })


async def api_dashboard_set_active_plugin(request: web.Request) -> web.Response:
    """POST {id: "..."} → pin a plugin as active for the picker."""
    from . import plugins_discovery
    try:
        payload = await request.json()
    except (ValueError, web.HTTPException):
        return web.json_response(
            {"ok": False, "error": "request body must be valid JSON"},
            status=400,
        )
    plugin_id = (payload or {}).get("id") or ""
    if not plugin_id:
        return web.json_response(
            {"ok": False, "error": "id is required"},
            status=400,
        )
    known = {p.id for p in plugins_discovery.list_plugins()}
    if plugin_id not in known:
        return web.json_response(
            {"ok": False, "error": "unknown plugin id"},
            status=400,
        )
    plugins_discovery.set_active_plugin(plugin_id)
    return web.json_response({"ok": True})


async def api_chat_projects_list(request: web.Request) -> web.Response:
    return web.json_response({"projects": projects.list_projects()})


async def api_fs_native_picker(request: web.Request) -> web.Response:
    """Pop the host OS's native folder-selection dialog.

    Browsers can't open Finder/Explorer/etc. directly (paths get
    stripped for security), but the dashboard runs locally on the
    user's own machine — so we can shell out to the platform's native
    picker. The user picks a folder in the real OS dialog and we hand
    the absolute path back to the JS modal.

    macOS:   osascript 'POSIX path of (choose folder)'
    Linux:   zenity --file-selection --directory (skipped if missing)
    Windows: powershell FolderBrowserDialog
    """
    import asyncio as _asyncio
    import sys as _sys
    import shutil as _shutil
    platform = _sys.platform
    cmd: list[str] | None = None
    if platform == "darwin":
        script = 'POSIX path of (choose folder with prompt "Choose a project folder")'
        cmd = ["osascript", "-e", script]
    elif platform.startswith("linux"):
        zenity = _shutil.which("zenity")
        if not zenity:
            return web.json_response(
                {"ok": False, "error": "zenity is not installed — paste the path or use Browse… instead"},
                status=400,
            )
        cmd = [zenity, "--file-selection", "--directory",
               "--title=Choose a project folder"]
    elif platform.startswith("win"):
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$d.Description = 'Choose a project folder';"
            "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }"
        )
        cmd = ["powershell", "-NoProfile", "-Command", ps]
    else:
        return web.json_response(
            {"ok": False, "error": f"native picker not supported on {platform}"},
            status=400,
        )
    try:
        proc = await _asyncio.create_subprocess_exec(
            *cmd,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
        )
        # Generous timeout — the user may stare at the picker for a while.
        try:
            stdout, stderr = await _asyncio.wait_for(proc.communicate(), timeout=300)
        except _asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return web.json_response(
                {"ok": False, "error": "native picker timed out"},
                status=504,
            )
    except FileNotFoundError as exc:
        return web.json_response(
            {"ok": False, "error": f"native picker binary missing: {exc.filename}"},
            status=500,
        )
    if proc.returncode != 0:
        # osascript exits 1 with "User canceled" on cancel; zenity exits 1
        # on cancel; PowerShell exits 0 with empty stdout. Treat all
        # cancel-shaped exits as a clean "no selection".
        err = (stderr.decode("utf-8", "replace") or "").strip()
        if "User canceled" in err or proc.returncode == 1 and not err:
            return web.json_response({"ok": True, "path": None})
        if "User canceled" in err:
            return web.json_response({"ok": True, "path": None})
        return web.json_response(
            {"ok": False, "error": err or f"picker exited with status {proc.returncode}"},
            status=400,
        )
    path = stdout.decode("utf-8", "replace").strip()
    if not path:
        # Empty output (PowerShell cancel path).
        return web.json_response({"ok": True, "path": None})
    # macOS POSIX path can include a trailing slash; strip for consistency.
    path = path.rstrip("/") or "/"
    return web.json_response({"ok": True, "path": path})


async def api_fs_listdir(request: web.Request) -> web.Response:
    """Directory listing for the in-modal folder browser.

    Returns the subdirectories of an absolute filesystem path. Defaults
    to the user's home directory when no ``path`` query param is given.
    Files are intentionally omitted from the response — the picker is
    for selecting *project folders*, not arbitrary files.
    """
    raw = request.query.get("path", "")
    target = Path(raw).expanduser() if raw else Path.home()
    if not target.is_absolute():
        return web.json_response(
            {"ok": False, "error": "path must be absolute"},
            status=400,
        )
    try:
        resolved = target.resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        return web.json_response(
            {"ok": False, "error": f"path not found: {target}"},
            status=404,
        )
    if not resolved.is_dir():
        return web.json_response(
            {"ok": False, "error": "path is not a directory"},
            status=400,
        )
    try:
        children = sorted(resolved.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        return web.json_response(
            {"ok": False, "error": f"can't list directory: {exc.strerror or exc!s}"},
            status=400,
        )
    entries: list[dict[str, Any]] = []
    for child in children:
        # Skip dotfiles by default; user can paste a path if they need one.
        if child.name.startswith("."):
            continue
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        if not is_dir:
            continue
        entries.append({"name": child.name, "path": str(child)})
    parent = str(resolved.parent) if resolved.parent != resolved else None
    return web.json_response({
        "ok": True,
        "path": str(resolved),
        "parent": parent,
        "home": str(Path.home()),
        "entries": entries,
    })


async def api_chat_sessions_list(request: web.Request) -> web.Response:
    """List prior sessions for a project (newest first; pinned on top)."""
    project_path = request.query.get("project_path", "")
    if not project_path:
        return web.json_response({"ok": False, "error": "project_path required"}, status=400)
    sessions = chat_mod.list_sessions(project_path)
    return web.json_response({"sessions": sessions})


async def api_chat_session_messages(request: web.Request) -> web.Response:
    """Replay payload — user+assistant text turns for a single session."""
    project_path = request.query.get("project_path", "")
    session_id = request.match_info.get("session_id", "")
    if not project_path or not session_id:
        return web.json_response(
            {"ok": False, "error": "project_path and session_id required"},
            status=400,
        )
    messages = chat_mod.read_session_messages(project_path, session_id)
    return web.json_response({"messages": messages})


async def api_chat_session_metadata(request: web.Request) -> web.Response:
    """Patch label / pinned / deleted for a session."""
    try:
        payload = await request.json()
    except (ValueError, web.HTTPException):
        return web.json_response(
            {"ok": False, "error": "request body must be valid JSON"},
            status=400,
        )
    if not isinstance(payload, dict):
        return web.json_response(
            {"ok": False, "error": "request body must be a JSON object"},
            status=400,
        )
    project_path = payload.get("project_path") or ""
    session_id = request.match_info.get("session_id", "")
    if not project_path or not session_id:
        return web.json_response(
            {"ok": False, "error": "project_path and session_id required"},
            status=400,
        )
    # Only allow writes for projects the user has already added (or
    # pinned defaults). Without this guard a stray POST could grow the
    # sessions store unboundedly with arbitrary keys.
    known_paths = {p["path"] for p in projects.list_projects()}
    if project_path not in known_paths:
        return web.json_response(
            {"ok": False, "error": "project_path is not a known chat project"},
            status=400,
        )
    if not chat_mod._is_valid_session_id(session_id):
        return web.json_response(
            {"ok": False, "error": "session_id must be UUID-shaped"},
            status=400,
        )
    label = payload.get("label")
    pinned = payload.get("pinned")
    deleted = payload.get("deleted")
    chat_mod.update_session_metadata(
        project_path,
        session_id,
        label=label if isinstance(label, str) else None,
        pinned=bool(pinned) if pinned is not None else None,
        deleted=bool(deleted) if deleted is not None else None,
    )
    return web.json_response({"ok": True})


async def api_chat_projects_add(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except (ValueError, web.HTTPException):
        return web.json_response(
            {"ok": False, "error": "request body must be valid JSON"},
            status=400,
        )
    if not isinstance(payload, dict):
        return web.json_response(
            {"ok": False, "error": "request body must be a JSON object"},
            status=400,
        )
    entry = projects.add_project(
        payload.get("path") or "",
        payload.get("label"),
    )
    if entry is None:
        return web.json_response(
            {"ok": False, "error": "path is not an existing directory"},
            status=400,
        )
    return web.json_response({"ok": True, "entry": entry})


async def api_chat_projects_remove(request: web.Request) -> web.Response:
    path = request.query.get("path", "")
    if not path:
        # Allow DELETE with a JSON body too, for clients that prefer.
        try:
            payload = await request.json()
            if isinstance(payload, dict):
                path = payload.get("path") or ""
        except (ValueError, web.HTTPException):
            pass
    if not path:
        return web.json_response(
            {"ok": False, "error": "'path' query string or JSON field is required"},
            status=400,
        )
    removed = projects.remove_project(path)
    return web.json_response({"ok": True, "removed": removed})


async def api_cache_invalidate(request: web.Request) -> web.Response:
    """Drop every in-process data-layer cache.

    Surfaced as ``POST /api/admin/cache-invalidate``. Gated by the M2
    host/origin middleware so only localhost can trigger it. Used by the
    edit-in-place write helpers and as a dev tool when on-disk content
    changes outside the dashboard.
    """
    cleared = data.cache_invalidate()
    return web.json_response({"ok": True, "cleared": cleared})


# --------------------------------------------------------------------------- #
# SSE event stream (preserved from old server)                                #
# --------------------------------------------------------------------------- #


async def sse_events(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)
    await response.write(b"event: connected\ndata: {}\n\n")
    tail = events.EventTail(events.EVENT_LOG)
    try:
        async for event in tail:
            payload = json.dumps(event).encode("utf-8")
            await response.write(b"data: " + payload + b"\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    return response


# --------------------------------------------------------------------------- #
# Template helpers                                                            #
# --------------------------------------------------------------------------- #


def _fmt_ts(value: Any) -> str:
    """Format a float/str epoch timestamp as ``HH:MM:SS``.

    Returns ``"—"`` for missing or unparseable input. Used as a Jinja
    filter so server-rendered timestamps in ``live.html`` / ``home.html``
    match the client-side rendering in ``studio.js``.
    """
    if value is None or value == "" or value == "—":
        return "—"
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return "—"
    try:
        import datetime
        return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return "—"


# --------------------------------------------------------------------------- #
# App factory                                                                 #
# --------------------------------------------------------------------------- #


def create_app() -> web.Application:
    app = web.Application(middlewares=[host_origin_guard])

    # Templates
    aiohttp_jinja2.setup(
        app,
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    env = aiohttp_jinja2.get_env(app)
    # Register the callable, not its current return value — each render
    # re-evaluates mtime so CSS/JS edits invalidate the cache key live.
    env.globals["asset_version"] = data.asset_version
    # Server-rendered timestamps go through this so they match the
    # HH:MM:SS form that studio.js uses for SSE-pushed events. Without
    # the filter, templates display raw float epochs (e.g. "1778779621.68").
    env.filters["fmt_ts"] = _fmt_ts

    # HTML pages
    app.router.add_get("/", page_home)
    app.router.add_get("/agents", page_agents)
    app.router.add_get("/agents/{id}", page_agent_detail)
    app.router.add_get("/skills", page_skills)
    app.router.add_get("/skills/{id}", page_skill_detail)
    app.router.add_get("/commands", page_commands)
    app.router.add_get("/commands/{name}", page_command_detail)
    app.router.add_get("/live", page_live)
    app.router.add_get("/graph", page_graph)
    app.router.add_get("/decision-trees", page_decision_trees)
    app.router.add_get("/decision-trees/{name}", page_decision_tree_detail)
    app.router.add_get("/chat", page_chat)
    app.router.add_get("/explore", page_explore)
    app.router.add_get("/settings", page_settings)

    # JSON APIs
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/running", api_running)
    app.router.add_get("/api/recent-events", api_recent_events)
    app.router.add_get("/api/graph", api_graph)
    app.router.add_get("/api/slash-options", api_slash_options)
    app.router.add_post("/api/admin/cache-invalidate", api_cache_invalidate)
    app.router.add_post("/api/edit/agent/{id}", api_edit_agent)
    app.router.add_post("/api/edit/skill/{id}", api_edit_skill)
    app.router.add_post("/api/edit/command/{name}", api_edit_command)
    app.router.add_post("/api/edit/decision-tree/{name}", api_edit_decision_tree)

    # Chat panel WebSocket (Phase C1+). The M2 host/origin middleware
    # gates the upgrade request just like every other route.
    app.router.add_get("/chat/ws", chat_mod.ws_chat_handler)

    # Chat panel project picker (Phase C2).
    app.router.add_get("/api/dashboard/plugins", api_dashboard_plugins)
    app.router.add_post("/api/dashboard/active-plugin", api_dashboard_set_active_plugin)
    app.router.add_get("/api/fs/listdir", api_fs_listdir)
    app.router.add_post("/api/fs/native-picker", api_fs_native_picker)
    app.router.add_get("/api/chat/projects", api_chat_projects_list)
    app.router.add_post("/api/chat/projects", api_chat_projects_add)
    app.router.add_get("/api/chat/sessions", api_chat_sessions_list)
    app.router.add_get("/api/chat/sessions/{session_id}/messages", api_chat_session_messages)
    app.router.add_post("/api/chat/sessions/{session_id}/meta", api_chat_session_metadata)
    app.router.add_delete("/api/chat/projects", api_chat_projects_remove)

    # SSE + legacy endpoints
    app.router.add_get("/events", sse_events)
    app.router.add_get("/health", api_health)  # legacy alias
    app.router.add_get("/skill-map.json", api_graph)  # legacy alias

    # Static files
    app.router.add_static("/static/", str(STATIC_DIR), name="static")

    # Plugin extensions — mount last so they can see (but not override)
    # the dashboard's own routes / templates / static files.
    from . import extensions
    extensions.mount_all(app)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="claude-code-dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument(
        "--migrate-legacy",
        action="store_true",
        help="Copy state from ~/.claude/ka-sfskills/ to ~/.claude/dashboard/ "
             "if any pre-v0.2 files are present. Idempotent and non-destructive.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")

    # Migration prompt: if the user has pre-v0.2 state at
    # ~/.claude/ka-sfskills/ AND no state yet at ~/.claude/dashboard/,
    # surface the option on every boot until they act. Auto-migrating
    # would be too magical; --migrate-legacy is the explicit opt-in.
    from . import paths as paths_mod
    if paths_mod.has_legacy_state() and not (Path.home() / ".claude" / "dashboard").exists():
        if args.migrate_legacy:
            results = paths_mod.migrate_from_legacy()
            logging.info("legacy state migration:")
            for name, status in results.items():
                logging.info("  %-22s %s", name, status)
        else:
            logging.warning(
                "Found legacy dashboard state at %s but no %s. "
                "Re-run with --migrate-legacy to copy the events log, "
                "chat sessions, and project list into the new location. "
                "Until then the dashboard continues to read from the legacy path.",
                paths_mod.legacy_data_dir(),
                Path.home() / ".claude" / "dashboard",
            )

    logging.info("claude-code-dashboard")
    logging.info("  event log: %s", events.EVENT_LOG)
    logging.info("  templates: %s", TEMPLATES_DIR)
    logging.info("  serving:   http://%s:%d", args.host, args.port)

    web.run_app(create_app(), host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
