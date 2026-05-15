"""Data loaders for the studio dashboard.

Single source of truth used by both HTML pages and JSON APIs in
``app.py``. Every function here reads from the existing on-disk sources
(see ``__init__.py`` docstring) — no new database, no new caches beyond
in-process memoization.

Where the MCP server already has a correct loader (``sfskills_mcp.agents``,
``sfskills_mcp.resources``, ``sfskills_mcp.skills``), we delegate to it.
The dashboard's reads are read-only so the cache invalidation footguns
flagged in the original Python review are inherited but not introduced
here.
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import yaml  # PyYAML — already required by requirements.txt for build_skill_map.py.
except ImportError:  # pragma: no cover — surfaced as parser fallback below.
    yaml = None  # type: ignore[assignment]

from . import events as events_mod

# --------------------------------------------------------------------------- #
# Repo root resolution                                                        #
# --------------------------------------------------------------------------- #


def _repo_root() -> Path:
    """Resolve the dashboard plugin's own install root.

    Always walks up from this file (scripts/dashboard/data.py →
    scripts/dashboard → scripts → plugin root). The previous
    implementation preferred sfskills_mcp.paths.repo_root() but that
    pre-split helper points at the user's ka-sfskills dev clone, not
    the dashboard plugin's install. Content paths (agents/skills/
    commands) come from the *active plugin's* root via
    _active_content_root() — never from this ROOT.
    """
    return Path(__file__).resolve().parents[2]


ROOT = _repo_root()


@lru_cache(maxsize=1)
def manifest() -> dict[str, Any]:
    """Load the active plugin's dashboard manifest.

    Reads ``.claude-plugin/dashboard.json`` from the repo root. Returns
    a partially-defaulted dict so callers can read fields like
    ``manifest()['content']['agents']['path']`` without null-checks.

    The manifest is the contract between the dashboard shell and a
    content plugin. ka-sfskills ships one as part of the v0.2 split;
    the schema is documented in ARCHITECTURE.md.
    """
    path = ROOT / ".claude-plugin" / "dashboard.json"
    defaults: dict[str, Any] = {
        "title": "ka-sfskills",
        "brand": {"mark": "k", "color": "#DA7756"},
        "content": {
            "agents": {"path": "agents", "pattern": "<id>/AGENT.md"},
            "skills": {"path": "skills", "pattern": "<domain>/<name>/SKILL.md"},
            "commands": {"path": "commands", "pattern": "<name>.md"},
            "decision_trees": {"path": "standards/decision-trees", "pattern": "<name>.md"},
            "templates": {"path": "agents/_shared/templates", "pattern": None},
        },
        "status_health": None,
        "taxonomy": {"colors": {}},
    }
    if not path.exists():
        return defaults
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    # Merge shallowly. We trust the manifest to be well-formed; missing
    # fields fall back to defaults.
    out = {**defaults, **loaded}
    # Merge content sub-dict so a partial override (e.g. only agents)
    # still picks up defaults for the other content kinds.
    if isinstance(loaded.get("content"), dict):
        out["content"] = {**defaults["content"], **loaded["content"]}
    return out


def _active_content_root() -> Path:
    """Where to read agents/skills/commands from.

    Resolution:
      1. The active plugin discovered in
         ``~/.claude/plugins/cache/`` (via
         plugins_discovery.active_plugin). This is the normal case
         post-split — the dashboard plugin doesn't ship content; the
         content lives in the host plugin.
      2. The dashboard's own ROOT, when no content plugin is installed
         yet. The dashboard pages render empty in that case but still
         load.
    """
    try:
        from . import plugins_discovery
        active = plugins_discovery.active_plugin()
    except Exception:  # noqa: BLE001 — defensive against bad manifest
        active = None
    return active.root if active is not None else ROOT


def _active_manifest() -> dict[str, Any]:
    """Return the active plugin's manifest, or the host's defaults."""
    try:
        from . import plugins_discovery
        active = plugins_discovery.active_plugin()
        if active is not None:
            return active.manifest
    except Exception:  # noqa: BLE001
        pass
    return manifest()


def _content_dir(kind: str) -> Path:
    """Resolve a content directory (agents/skills/commands/etc.) from
    the active plugin's manifest, relative to its root."""
    m = _active_manifest()
    entry = (m.get("content") or {}).get(kind, {})
    rel = entry.get("path") or kind
    return _active_content_root() / rel


# Module-level constants kept for backward compat. They snapshot the
# active plugin's content layout at import time. If the user switches
# plugins via the picker the page reload re-imports the module.
AGENTS_DIR = _content_dir("agents")
COMMANDS_DIR = _content_dir("commands")
SKILLS_DIR = _content_dir("skills")
DECISION_TREES_DIR = _content_dir("decision_trees")
REGISTRY_PATH = ROOT / "registry" / "skills.json"
BUILD_SKILL_MAP = ROOT / "scripts" / "build_skill_map.py"


# --------------------------------------------------------------------------- #
# Asset versioning (cache-bust CSS/JS in dev)                                 #
# --------------------------------------------------------------------------- #


def asset_version() -> str:
    """Cache-buster for CSS/JS in <link>/<script> tags.

    Uses the mtime of studio.css when available so edits invalidate caches
    without a manual bump.
    """
    css = Path(__file__).resolve().parent / "static" / "studio.css"
    if css.exists():
        return str(int(css.stat().st_mtime))
    return str(int(time.time()))


# --------------------------------------------------------------------------- #
# Cache invalidation                                                          #
# --------------------------------------------------------------------------- #


def cache_invalidate() -> dict[str, int]:
    """Drop every in-process data-layer cache.

    Called by ``POST /api/admin/cache-invalidate`` and by the edit-in-place
    write helpers so a save immediately reflects on subsequent reads. Each
    loader is wrapped in ``@lru_cache(maxsize=1)`` (a known footgun flagged
    in the original code review); without this nuke option, edits made via
    the dashboard wouldn't surface until the server restarted.

    Returns a ``{name: cache_size_before_clear}`` map for observability.
    """
    cleared: dict[str, int] = {}
    for name, fn in [
        ("_agents_index", _agents_index),
        ("_registry", _registry),
        ("_commands_index", _commands_index),
        ("_trees_index", _trees_index),
    ]:
        info = fn.cache_info()
        cleared[name] = info.currsize
        fn.cache_clear()
    # Also nuke the upstream sfskills_mcp caches we depend on indirectly.
    try:
        from sfskills_mcp import paths as _paths
        if hasattr(_paths.repo_root, "cache_clear"):
            _paths.repo_root.cache_clear()
            cleared["sfskills_mcp.paths.repo_root"] = 0
    except ImportError:
        pass
    return cleared


# --------------------------------------------------------------------------- #
# Agents                                                                      #
# --------------------------------------------------------------------------- #

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.S)


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse the leading ``---``-fenced YAML block from a markdown document.

    Uses PyYAML — already required by ``requirements.txt`` for
    ``build_skill_map.py``. Returns ``{}`` on missing / malformed
    frontmatter so a single broken agent doesn't crash a page.

    Earlier this function was a hand-rolled stdlib-only parser. It
    silently flattened nested mappings (``dependencies.skills``) into
    top-level keys, corrupting 72 of 74 AGENT.md files. Switched to
    ``yaml.safe_load`` after that surfaced in a code review.
    """
    m = _FRONTMATTER.match(text)
    if not m:
        return {}
    if yaml is None:
        return {}
    try:
        parsed = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


_AGENT_CATEGORIES = {
    "apex": ["apex-builder", "apex-refactorer", "trigger-consolidator", "test-class-generator",
             "soql-optimizer", "scan-security", "security-scanner"],
    "lwc": ["lwc-builder", "lwc-auditor", "lwc-debugger"],
    "flow": ["flow-builder", "build-flow", "flow-analyzer", "analyze-flow", "flow-orchestrator-designer",
             "automation-migration-router"],
    "security": ["permission-set-architect", "audit-sharing", "sharing-audit-agent",
                 "my-domain-and-session-security-auditor", "duplicate-rule-designer",
                 "field-audit-trail-and-history-tracking-governor"],
    "data": ["bulk-migration-planner", "csv-to-object-mapper", "data-loader-pre-flight",
             "data-model-reviewer", "field-impact-analyzer"],
    "admin": ["object-designer", "design-object", "custom-metadata-and-settings-designer",
              "business-hours-and-holidays-configurator", "entitlement-and-milestone-designer",
              "lead-routing-rules-designer", "omni-channel-routing-designer",
              "assignment-and-auto-response-rules-designer", "path-designer",
              "sales-stage-designer", "knowledge-article-taxonomy-agent",
              "experience-cloud-admin-designer", "config-workbook-author",
              "process-flow-mapper", "email-template-modernizer"],
    "audit": ["audit-router", "case-escalation-auditor", "lightning-record-page-auditor",
              "list-view-and-search-layout-auditor", "picklist-governor",
              "prompt-library-governor", "record-type-and-layout-auditor",
              "report-and-dashboard-auditor", "reports-and-dashboards-folder-sharing-auditor",
              "quick-action-and-global-action-auditor", "validation-rule-auditor",
              "org-drift-detector"],
    "rca": ["rca-asset-lifecycle-tracker", "rca-billing-designer", "rca-catalog-drift-detector",
            "rca-cpq-migration-planner", "rca-forecasting-integrator",
            "rca-multi-currency-architect", "rca-pricing-architect",
            "rca-pricing-rules-auditor", "rca-product-catalog-designer",
            "rca-product-rules-auditor", "rca-quote-to-order-designer",
            "rca-revenue-recognition-designer", "rca-tax-engine-integrator"],
    "devops": ["changeset-builder", "deployment-risk-scorer", "release-train-planner",
               "sandbox-strategy-designer", "integration-catalog-builder"],
    "strategy": ["assess-org", "fit-gap-analyzer", "story-drafter", "waf-assessor",
                 "run-fit-gap", "user-access-diff", "profile-to-permset-migrator"],
    "agentforce": ["agentforce-builder", "agentforce-action-reviewer", "build-agentforce-action",
                   "review-agentforce-action"],
}


def _classify_agent(agent_id: str) -> str:
    for cat, ids in _AGENT_CATEGORIES.items():
        if agent_id in ids:
            return cat
    if agent_id.startswith("rca-"):
        return "rca"
    return "other"


def _first_paragraph(markdown: str, heading: str) -> str:
    """First non-empty paragraph after ``## <heading>``. Falls back to the
    document's first non-heading paragraph."""
    h = re.search(rf"^##\s+{re.escape(heading)}\s*$", markdown, re.M)
    body = markdown[h.end():] if h else markdown
    for chunk in re.split(r"\n\s*\n", body.strip()):
        chunk = chunk.strip()
        if chunk and not chunk.startswith("#"):
            return chunk
    return ""


@lru_cache(maxsize=1)
def _agents_index() -> list[dict[str, Any]]:
    """Scan agents/ and return one record per runtime agent."""
    out: list[dict[str, Any]] = []
    if not AGENTS_DIR.exists():
        return out
    for entry in sorted(AGENTS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        md = entry / "AGENT.md"
        if not md.exists():
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        if fm.get("class") and fm["class"] != "runtime":
            continue
        if fm.get("status") == "deprecated":
            continue
        summary = _first_paragraph(text, "What This Agent Does")
        suggested = re.findall(r"`(skills/[^`]+)`", text)
        out.append({
            "id": entry.name,
            "name": entry.name,
            "category": _classify_agent(entry.name),
            "summary": summary[:300],
            "suggested_skill_count": len(set(suggested)),
            "path": str(md.relative_to(_active_content_root())),
        })
    return out


def list_agents(category: str = "all", sort: str = "name") -> list[dict[str, Any]]:
    items = list(_agents_index())
    if category and category != "all":
        items = [a for a in items if a["category"] == category]
    if sort == "skills":
        items.sort(key=lambda a: -a["suggested_skill_count"])
    else:
        items.sort(key=lambda a: a["name"])
    return items


def runtime_agent_count() -> int:
    return len(_agents_index())


def agent_categories() -> list[dict[str, Any]]:
    counts: Counter[str] = Counter(a["category"] for a in _agents_index())
    out = [{"slug": "all", "count": sum(counts.values())}]
    for slug in sorted(counts):
        out.append({"slug": slug, "count": counts[slug]})
    return out


def get_agent(agent_id: str) -> dict[str, Any] | None:
    if not re.match(r"^[a-zA-Z0-9][\w-]*$", agent_id or ""):
        return None
    md = AGENTS_DIR / agent_id / "AGENT.md"
    try:
        md.resolve().relative_to(AGENTS_DIR.resolve())
    except (OSError, ValueError):
        return None
    if not md.exists():
        return None
    text = md.read_text(encoding="utf-8")
    suggested = re.findall(r"`(skills/[^`]+)`", text)
    templates = re.findall(r"`(templates/[^`]+)`", text)
    return {
        "id": agent_id,
        "name": agent_id,
        "category": _classify_agent(agent_id),
        "summary": _first_paragraph(text, "What This Agent Does")[:500],
        "markdown": text,
        "suggested_skills": sorted(set(suggested)),
        "templates": sorted(set(templates)),
    }


# --------------------------------------------------------------------------- #
# Skills                                                                      #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _registry() -> dict[str, Any]:
    if not REGISTRY_PATH.exists():
        return {"skills": [], "domain_counts": {}, "skill_count": 0}
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"skills": [], "domain_counts": {}, "skill_count": 0}


def skill_count() -> int:
    return _registry().get("skill_count", 0)


def skill_domains() -> list[dict[str, Any]]:
    counts = _registry().get("domain_counts", {})
    out = [{"slug": "all", "count": sum(counts.values())}]
    for slug in sorted(counts):
        out.append({"slug": slug, "count": counts[slug]})
    return out


def list_skills(
    domain: str = "all", q: str = "", limit: int = 120
) -> tuple[list[dict[str, Any]], int]:
    all_skills = _registry().get("skills", [])
    if domain and domain != "all":
        filtered = [s for s in all_skills if s.get("category") == domain]
    else:
        filtered = list(all_skills)
    if q:
        needle = q.lower()
        filtered = [
            s for s in filtered
            if needle in (s.get("id") or "").lower()
            or needle in (s.get("name") or "").lower()
            or needle in (s.get("description") or "").lower()
        ]
    return filtered[:limit], len(filtered)


def get_skill(skill_id: str) -> dict[str, Any] | None:
    if "/" not in skill_id:
        return None
    domain, _, name = skill_id.partition("/")
    if not (re.match(r"^[a-zA-Z0-9_-]+$", domain) and re.match(r"^[a-zA-Z0-9_-]+$", name)):
        return None
    skill_dir = SKILLS_DIR / domain / name
    md = skill_dir / "SKILL.md"
    if not md.exists():
        return None
    text = md.read_text(encoding="utf-8")
    registry_record = next(
        (s for s in _registry().get("skills", []) if s.get("id") == skill_id),
        {},
    )
    references = []
    refs_dir = skill_dir / "references"
    if refs_dir.exists():
        references = sorted(p.stem for p in refs_dir.glob("*.md"))
    templates = []
    templ_dir = skill_dir / "templates"
    if templ_dir.exists():
        templates = sorted(str(p.relative_to(skill_dir)) for p in templ_dir.rglob("*") if p.is_file())
    return {
        "id": skill_id,
        "domain": domain,
        "name": name,
        "description": registry_record.get("description", ""),
        "markdown": text,
        "references": references,
        "templates": templates,
    }


# --------------------------------------------------------------------------- #
# Commands                                                                    #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _commands_index() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not COMMANDS_DIR.exists():
        return out
    for entry in sorted(COMMANDS_DIR.iterdir()):
        if entry.suffix != ".md" or entry.stem.startswith("_"):
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"^#\s+(.+)$", text, re.M)
        title = m.group(1).strip() if m else entry.stem
        # Take the first non-heading paragraph as description.
        desc = ""
        for chunk in re.split(r"\n\s*\n", text):
            chunk = chunk.strip()
            if chunk and not chunk.startswith("#") and not chunk.startswith("---"):
                desc = chunk.split("\n")[0]
                break
        out.append({"name": entry.stem, "title": title, "description": desc[:240]})
    return out


def list_commands() -> list[dict[str, Any]]:
    return list(_commands_index())


def command_count() -> int:
    return len(_commands_index())


def get_command(name: str) -> dict[str, Any] | None:
    if not re.match(r"^[a-zA-Z0-9_-]+$", name or ""):
        return None
    md = COMMANDS_DIR / f"{name}.md"
    try:
        md.resolve().relative_to(COMMANDS_DIR.resolve())
    except (OSError, ValueError):
        return None
    if not md.exists():
        return None
    text = md.read_text(encoding="utf-8")
    m = re.search(r"^#\s+(.+)$", text, re.M)
    title = m.group(1).strip() if m else name
    return {"name": name, "title": title, "markdown": text}


# --------------------------------------------------------------------------- #
# Decision trees                                                              #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _trees_index() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not DECISION_TREES_DIR.exists():
        return out
    for entry in sorted(DECISION_TREES_DIR.iterdir()):
        if entry.suffix != ".md" or entry.stem == "README":
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"^#\s+(.+)$", text, re.M)
        title = m.group(1).strip() if m else entry.stem
        out.append({"name": entry.stem, "title": title})
    return out


def list_decision_trees() -> list[dict[str, Any]]:
    return list(_trees_index())


def get_decision_tree(name: str) -> dict[str, Any] | None:
    if not re.match(r"^[a-zA-Z0-9_-]+$", name or ""):
        return None
    md = DECISION_TREES_DIR / f"{name}.md"
    try:
        md.resolve().relative_to(DECISION_TREES_DIR.resolve())
    except (OSError, ValueError):
        return None
    if not md.exists():
        return None
    text = md.read_text(encoding="utf-8")
    m = re.search(r"^#\s+(.+)$", text, re.M)
    title = m.group(1).strip() if m else name
    return {"name": name, "title": title, "markdown": text}


# --------------------------------------------------------------------------- #
# Live activity                                                               #
# --------------------------------------------------------------------------- #


def recent_events(limit: int = 30) -> list[dict[str, Any]]:
    return events_mod.read_recent_events(events_mod.EVENT_LOG, limit)


def running_agents() -> list[dict[str, Any]]:
    """Best-effort: subagent_starting events that don't have a matching
    subagent_completed within the recent window."""
    raw = events_mod.read_recent_events(events_mod.EVENT_LOG, 500)
    starts: dict[str, dict[str, Any]] = {}
    for e in raw:
        t = e.get("type", "")
        if t == "subagent_starting" or t == "agent_dispatch":
            # Hooks emit ``subagent_type`` (see .claude/hooks/track-subagent.py);
            # we tolerate other field names in case other emitters appear.
            key = (
                e.get("subagent_type")
                or e.get("subagent_id")
                or e.get("agent_id")
                or e.get("name")
                or ""
            )
            if key:
                starts[key] = e
        elif t in {"subagent_completed", "agent_complete"}:
            # Hooks emit ``subagent_type`` (see .claude/hooks/track-subagent.py);
            # we tolerate other field names in case other emitters appear.
            key = (
                e.get("subagent_type")
                or e.get("subagent_id")
                or e.get("agent_id")
                or e.get("name")
                or ""
            )
            starts.pop(key, None)
    out = []
    for key, e in starts.items():
        out.append({
            "id": key,
            "name": e.get("agent_id") or e.get("subagent_type") or e.get("name") or key,
            "started_at": e.get("ts") or e.get("timestamp"),
            "description": (e.get("description") or "")[:200],
        })
    return out


def recent_completed_agents(limit: int = 3) -> list[dict[str, Any]]:
    raw = events_mod.read_recent_events(events_mod.EVENT_LOG, 200)
    completed = [e for e in raw if e.get("type") in {"subagent_completed", "agent_complete"}]
    return completed[-limit:][::-1]


def top_skills(window_seconds: int = 3600, limit: int = 8) -> list[dict[str, Any]]:
    raw = events_mod.read_recent_events(events_mod.EVENT_LOG, 2000)
    cutoff = time.time() - window_seconds
    counts: Counter[str] = Counter()
    for e in raw:
        if e.get("type") != "skill_accessed" and e.get("type") != "skill_read":
            continue
        ts = e.get("ts") or e.get("timestamp") or 0
        if isinstance(ts, str):
            try:
                ts = float(ts)
            except ValueError:
                ts = 0
        if ts and ts < cutoff:
            continue
        skill_id = e.get("skill_id") or e.get("skill_path") or ""
        if skill_id:
            counts[skill_id] += 1
    most = counts.most_common(limit)
    max_count = most[0][1] if most else 1
    return [
        {"skill_id": sid, "reads": n, "bar_pct": round(100 * n / max_count)}
        for sid, n in most
    ]


def live_stats() -> dict[str, Any]:
    raw = events_mod.read_recent_events(events_mod.EVENT_LOG, 1000)
    now = time.time()
    last_hour = now - 3600
    last_5min = now - 300
    tool_calls = sum(
        1 for e in raw
        if e.get("type") == "mcp_tool_call" and _ts(e) >= last_5min
    )
    skill_reads = sum(
        1 for e in raw
        if (e.get("type") == "skill_accessed" or e.get("type") == "skill_read")
        and _ts(e) >= last_hour
    )
    completed_today = sum(
        1 for e in raw
        if e.get("type") in {"subagent_completed", "agent_complete"}
        and _ts(e) >= now - 86400
    )
    return {
        "running": len(running_agents()),
        "tool_calls_per_5min": tool_calls,
        "skill_reads_per_hour": skill_reads,
        "completed_today": completed_today,
    }


def _ts(event: dict[str, Any]) -> float:
    ts = event.get("ts") or event.get("timestamp") or 0
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return float(ts)
    except (ValueError, TypeError):
        return 0.0


# --------------------------------------------------------------------------- #
# Graph                                                                       #
# --------------------------------------------------------------------------- #


def get_graph(rebuild: bool = False) -> dict[str, Any]:
    path = events_mod.SKILL_MAP_PATH
    if rebuild or not path.exists():
        if BUILD_SKILL_MAP.exists():
            try:
                subprocess.run(
                    [sys.executable, str(BUILD_SKILL_MAP)],
                    check=True, capture_output=True, text=True, timeout=20,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                return {"error": f"failed to build skill_map.json: {exc}"}
        if not path.exists():
            return {"error": "skill_map.json not found and build_skill_map.py is unavailable"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": f"could not read skill_map.json: {exc}"}


# --------------------------------------------------------------------------- #
# Universal search                                                            #
# --------------------------------------------------------------------------- #


def universal_search(q: str, limit_per_kind: int = 6) -> dict[str, list[dict[str, Any]]]:
    needle = q.lower()
    agents = [
        {"id": a["id"], "name": a["name"], "category": a["category"], "summary": a["summary"][:140]}
        for a in _agents_index()
        if needle in a["id"].lower() or needle in (a["summary"] or "").lower()
    ][:limit_per_kind]
    skills, _ = list_skills(q=q, limit=limit_per_kind)
    skills_out = [
        {"id": s.get("id"), "domain": s.get("category"), "description": (s.get("description") or "")[:140]}
        for s in skills
    ]
    commands = [
        {"name": c["name"], "title": c["title"], "description": c["description"]}
        for c in _commands_index()
        if needle in c["name"].lower() or needle in (c["description"] or "").lower()
    ][:limit_per_kind]
    trees = [
        {"name": t["name"], "title": t["title"]}
        for t in _trees_index()
        if needle in t["name"].lower() or needle in (t["title"] or "").lower()
    ][:limit_per_kind]
    runs = [
        {"id": r["id"], "name": r["name"], "started_at": r["started_at"]}
        for r in running_agents()
        if needle in (r["name"] or "").lower() or needle in (r["id"] or "").lower()
    ][:limit_per_kind]
    return {"agents": agents, "skills": skills_out, "commands": commands, "decision_trees": trees, "runs": runs}


# --------------------------------------------------------------------------- #
# Settings snapshot                                                           #
# --------------------------------------------------------------------------- #


def settings_snapshot() -> dict[str, Any]:
    return {
        "repo_root": str(ROOT),
        "event_log": str(events_mod.EVENT_LOG),
        "event_log_exists": events_mod.EVENT_LOG.exists(),
        "registry_path": str(REGISTRY_PATH),
        "registry_path_exists": REGISTRY_PATH.exists(),
    }
