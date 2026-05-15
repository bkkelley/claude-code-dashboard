"""Write helpers for the edit-in-place feature.

Each ``write_<kind>`` function:

1. Validates the id with the same containment guard used by the read path.
2. For agents: parses the frontmatter with PyYAML and requires a ``class``
   key — silent corruption (the original ``_parse_frontmatter`` bug) is
   exactly what edit-in-place needs to surface as a 400.
3. Writes atomically via ``tempfile`` + ``os.replace`` so a crash mid-write
   never leaves a half-written ``AGENT.md`` on disk.
4. Calls ``data.cache_invalidate()`` so the next page render sees the new
   content without restarting the server.
5. For agents: invokes ``scripts/gen_subagents.py --agent <id>`` to keep
   the ``.claude/agents/<id>.md`` wrapper in sync with the canonical
   source. Warn-only — a generator failure does NOT roll back the write.

All functions return a ``WriteResult`` dataclass; route handlers turn
that into JSON.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from . import data

MAX_CONTENT_BYTES = 1_000_000  # 1MB — anything larger is almost certainly wrong input.
_AGENT_NAME = re.compile(r"^[a-zA-Z0-9][\w-]*$")
_SKILL_ID = re.compile(r"^[a-zA-Z0-9][\w-]*/[a-zA-Z0-9][\w-]*$")
_COMMAND_NAME = re.compile(r"^[a-zA-Z0-9][\w-]*$")
_TREE_NAME = re.compile(r"^[a-zA-Z0-9][\w-]*$")


@dataclass
class WriteResult:
    ok: bool
    path: str = ""
    mtime: float = 0.0
    error: str = ""
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Generic atomic write                                                        #
# --------------------------------------------------------------------------- #


def _atomic_write(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically.

    tempfile + ``os.replace`` guarantees an interrupted write never leaves a
    partial file. Same shape as ``meta.emit_envelope`` in the MCP server.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Agent writes                                                                #
# --------------------------------------------------------------------------- #


def _validate_agent_content(content: str) -> str | None:
    """Return an error message if ``content`` is unfit to land as an AGENT.md.

    Checks:
    - Non-empty
    - Has a YAML frontmatter block
    - Frontmatter parses as a dict
    - Frontmatter has a ``class`` key (the field that drives whether the
      agent shows up in the dashboard's agents page)
    """
    if not content.strip():
        return "AGENT.md cannot be empty"
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        return f"AGENT.md exceeds size limit ({MAX_CONTENT_BYTES} bytes)"
    fm_match = re.match(r"^---\n(.*?)\n---\n", content, re.S)
    if not fm_match:
        return "AGENT.md must start with a YAML frontmatter block (---\\n...\\n---)"
    if yaml is None:
        return None  # No validator available — let it through.
    try:
        parsed = yaml.safe_load(fm_match.group(1))
    except yaml.YAMLError as exc:
        return f"frontmatter is not valid YAML: {exc}"
    if not isinstance(parsed, dict):
        return "frontmatter must be a YAML mapping"
    if "class" not in parsed:
        return "frontmatter is missing required key: class"
    return None


def write_agent(agent_id: str, content: str) -> WriteResult:
    if not _AGENT_NAME.match(agent_id or ""):
        return WriteResult(ok=False, error=f"invalid agent id: {agent_id!r}")

    err = _validate_agent_content(content)
    if err:
        return WriteResult(ok=False, error=err)

    target = data.AGENTS_DIR / agent_id / "AGENT.md"
    try:
        # resolve + containment — never write outside agents/.
        target.parent.resolve().relative_to(data.AGENTS_DIR.resolve())
    except (OSError, ValueError):
        return WriteResult(ok=False, error=f"path resolves outside agents/: {agent_id!r}")
    if not target.parent.is_dir():
        return WriteResult(
            ok=False,
            error=f"agent directory does not exist: agents/{agent_id}/ "
            "(creating new agents from the dashboard isn't supported yet)",
        )

    try:
        _atomic_write(target, content)
    except OSError as exc:
        return WriteResult(ok=False, error=f"write failed: {exc}")

    data.cache_invalidate()
    result = WriteResult(
        ok=True,
        path=str(target.relative_to(data.ROOT)),
        mtime=target.stat().st_mtime,
    )

    # Best-effort: regenerate the .claude/agents/<id>.md wrapper. Same flow
    # that .claude/hooks/post-edit-dispatch.py runs when Claude Code edits
    # the file. Failures here are warnings, not write failures.
    regen_warning = _regen_subagent_wrapper(agent_id)
    if regen_warning:
        result.warnings.append(regen_warning)
    return result


def _regen_subagent_wrapper(agent_id: str) -> str | None:
    """Run ``scripts/gen_subagents.py --agent <id>`` with a short timeout.

    Returns a warning string on failure, ``None`` on success or when the
    generator script is missing.
    """
    script = data.ROOT / "scripts" / "gen_subagents.py"
    if not script.exists():
        return None
    out_path = data.ROOT / ".claude" / "agents" / f"{agent_id}.md"
    try:
        completed = subprocess.run(
            [
                sys.executable, str(script),
                "--agent", agent_id,
                "--out", str(out_path.relative_to(data.ROOT)),
            ],
            cwd=data.ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.SubprocessError as exc:
        return f"wrapper regen failed: {exc}"
    if completed.returncode != 0:
        return f"wrapper regen exited {completed.returncode}: {(completed.stderr or '').strip()[:200]}"
    return None


# --------------------------------------------------------------------------- #
# Skill writes                                                                #
# --------------------------------------------------------------------------- #


def _validate_skill_content(content: str) -> str | None:
    if not content.strip():
        return "SKILL.md cannot be empty"
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        return f"SKILL.md exceeds size limit ({MAX_CONTENT_BYTES} bytes)"
    return None


def write_skill(skill_id: str, content: str) -> WriteResult:
    if not _SKILL_ID.match(skill_id or ""):
        return WriteResult(ok=False, error=f"invalid skill id: {skill_id!r}")

    err = _validate_skill_content(content)
    if err:
        return WriteResult(ok=False, error=err)

    domain, _, name = skill_id.partition("/")
    target = data.SKILLS_DIR / domain / name / "SKILL.md"
    try:
        target.parent.resolve().relative_to(data.SKILLS_DIR.resolve())
    except (OSError, ValueError):
        return WriteResult(ok=False, error=f"path resolves outside skills/: {skill_id!r}")
    if not target.parent.is_dir():
        return WriteResult(
            ok=False,
            error=f"skill directory does not exist: skills/{skill_id}/ "
            "(creating new skills from the dashboard isn't supported yet)",
        )

    try:
        _atomic_write(target, content)
    except OSError as exc:
        return WriteResult(ok=False, error=f"write failed: {exc}")

    data.cache_invalidate()
    return WriteResult(
        ok=True,
        path=str(target.relative_to(data.ROOT)),
        mtime=target.stat().st_mtime,
    )


# --------------------------------------------------------------------------- #
# Command writes                                                              #
# --------------------------------------------------------------------------- #


def write_command(name: str, content: str) -> WriteResult:
    if not _COMMAND_NAME.match(name or ""):
        return WriteResult(ok=False, error=f"invalid command name: {name!r}")
    if not content.strip():
        return WriteResult(ok=False, error="command file cannot be empty")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        return WriteResult(ok=False, error=f"command file exceeds size limit ({MAX_CONTENT_BYTES} bytes)")

    target = data.COMMANDS_DIR / f"{name}.md"
    try:
        target.resolve().relative_to(data.COMMANDS_DIR.resolve())
    except (OSError, ValueError):
        return WriteResult(ok=False, error=f"path resolves outside commands/: {name!r}")

    try:
        _atomic_write(target, content)
    except OSError as exc:
        return WriteResult(ok=False, error=f"write failed: {exc}")

    data.cache_invalidate()
    return WriteResult(
        ok=True,
        path=str(target.relative_to(data.ROOT)),
        mtime=target.stat().st_mtime,
    )


# --------------------------------------------------------------------------- #
# Decision-tree writes                                                        #
# --------------------------------------------------------------------------- #


def write_decision_tree(name: str, content: str) -> WriteResult:
    if not _TREE_NAME.match(name or ""):
        return WriteResult(ok=False, error=f"invalid decision-tree name: {name!r}")
    if not content.strip():
        return WriteResult(ok=False, error="decision-tree file cannot be empty")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        return WriteResult(ok=False, error=f"decision-tree exceeds size limit ({MAX_CONTENT_BYTES} bytes)")

    target = data.DECISION_TREES_DIR / f"{name}.md"
    try:
        target.resolve().relative_to(data.DECISION_TREES_DIR.resolve())
    except (OSError, ValueError):
        return WriteResult(ok=False, error=f"path resolves outside decision-trees/: {name!r}")

    try:
        _atomic_write(target, content)
    except OSError as exc:
        return WriteResult(ok=False, error=f"write failed: {exc}")

    data.cache_invalidate()
    return WriteResult(
        ok=True,
        path=str(target.relative_to(data.ROOT)),
        mtime=target.stat().st_mtime,
    )


def result_to_response(result: WriteResult) -> tuple[dict[str, Any], int]:
    """Serialize a ``WriteResult`` for an aiohttp JSON response.

    Returns ``(body_dict, http_status)``. Validation errors get a 400,
    write failures a 500, success a 200.
    """
    if result.ok:
        return (
            {
                "ok": True,
                "path": result.path,
                "mtime": result.mtime,
                "warnings": result.warnings,
            },
            200,
        )
    # Distinguish validation errors (4xx) from infrastructure errors (5xx).
    if result.error and result.error.startswith("write failed"):
        return ({"ok": False, "error": result.error}, 500)
    return ({"ok": False, "error": result.error}, 400)
