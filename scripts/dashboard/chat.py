"""Chat subprocess manager — drives a real ``claude`` CLI instance.

Each chat session in the dashboard side panel is backed by a long-lived
``claude -p --output-format stream-json --input-format stream-json``
subprocess. User messages are written as JSONL to the subprocess's
stdin; Claude's stream-json events flow back on stdout and are parsed
into Python dicts.

Why a real subprocess (not the Anthropic API directly) — the user
inherits their full Claude Code context: project ~/.claude config, MCP
servers (including ka-sfskills's), every plugin slash command, hooks,
the works. This is what makes the dashboard chat an *extension* of
Claude Code rather than a separate chat client. See
``CLAUDE_CLI_NOTES.md`` for the spike findings that informed this
design.

Lifecycle:

1. Construct ``ChatSession(project_path, ...)``.
2. ``await session.spawn()`` launches the subprocess.
3. Iterate ``session.iter_events()`` from one coroutine and call
   ``session.send_message(text)`` from another as user input arrives.
4. ``await session.terminate()`` on disconnect — closes stdin (graceful
   EOF), waits 3s, SIGTERM, waits 2s, SIGKILL.

The ``claude`` binary is resolved from ``SFSKILLS_CLAUDE_BIN`` env var
when set (test injection), otherwise from ``shutil.which("claude")``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from . import paths as paths_mod
from . import projects as projects_mod

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Session persistence — last (project → session_id) for resume-on-reload      #
# --------------------------------------------------------------------------- #

# Backward-compat alias. Tests prior to the v0.2 split mutated
# ``chat_mod.SESSIONS_PATH`` directly to redirect state; new code +
# new tests should set ``paths_mod._data_dir_override`` instead, which
# redirects every dashboard state file in one statement. The module
# attribute is still set here so anything importing
# ``SESSIONS_PATH`` as a constant gets a sane default — internal
# code in this file uses ``_sessions_path()`` for dynamic dispatch.
SESSIONS_PATH = paths_mod.sessions_path()


def _sessions_path() -> "Path":
    """Return the active sessions-store path.

    Honors a runtime-mutated ``chat_mod.SESSIONS_PATH`` (legacy test
    pattern) by checking whether it diverged from the default, else
    falls through to paths_mod which respects
    ``_data_dir_override`` and ``KA_DASHBOARD_DATA_DIR``.
    """
    import sys as _sys
    mod = _sys.modules[__name__]
    attr = mod.__dict__.get("SESSIONS_PATH")
    default = paths_mod.sessions_path()
    return attr if (attr is not None and attr != default) else default


def _read_sessions() -> dict[str, Any]:
    """Read the session-persistence file. Tolerant of missing/corrupt."""
    path = _sessions_path()
    if not path.exists():
        return {"by_project": {}}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"by_project": {}}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("by_project"), dict):
        return {"by_project": {}}
    return parsed


def _write_sessions(payload: dict[str, Any]) -> None:
    """Atomic write — tempfile + os.replace, same shape as edit.py."""
    path = _sessions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
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


def get_last_session(project_path: str) -> dict[str, Any] | None:
    """Return ``{session_id, mode, model, last_used}`` for a project, or None."""
    store = _read_sessions()
    return store["by_project"].get(project_path)


def save_session(
    project_path: str, session_id: str, mode: str, model: str
) -> None:
    """Persist the last session for ``project_path``. Called on every ``ready``.

    Stores enough state to ``--resume`` the conversation on the next
    page reload — losing it would mean every refresh restarts from
    scratch, which the user explicitly asked us to avoid.

    Merges into the existing per-project dict instead of replacing it,
    so per-session metadata written by ``update_session_metadata``
    (label, pinned, deleted) survives this update.
    """
    store = _read_sessions()
    entry = store["by_project"].setdefault(project_path, {})
    entry.update({
        "session_id": session_id,
        "mode": mode,
        "model": model,
        "last_used": time.time(),
    })
    _write_sessions(store)


# --------------------------------------------------------------------------- #
# Session history — claude writes a JSONL transcript per session under         #
# ~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl. We read those         #
# directly to produce the history picker and the message-replay payload.       #
# --------------------------------------------------------------------------- #


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _sanitize_project_path(project_path: str) -> str:
    """Mirror Claude Code's session-dir naming: '/' and '.' → '-'.

    Example: ``/Users/blake/Documents/foo.git`` → ``-Users-blake-Documents-foo-git``.
    """
    if not project_path:
        return ""
    cleaned = project_path.replace("/", "-").replace(".", "-")
    return cleaned


def _session_dir_for(project_path: str) -> Path:
    return CLAUDE_PROJECTS_DIR / _sanitize_project_path(project_path)


def _read_first_user_text(jsonl_path: Path, char_cap: int = 120) -> str:
    """Pull the first user message text from a session jsonl, capped."""
    try:
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("type") != "user":
                    continue
                msg = evt.get("message") or {}
                content = msg.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    text = ""
                text = (text or "").strip()
                if text:
                    return text[:char_cap] + ("…" if len(text) > char_cap else "")
    except OSError:
        return ""
    return ""


def list_sessions(project_path: str, limit: int = 50) -> list[dict[str, Any]]:
    """Enumerate prior sessions for a project, newest first.

    Pulls from claude's transcript dir; merges in our own per-session
    metadata (label, pinned) from chat-sessions.json.

    Performance: we stat all files, sort + slice to ``limit`` first, then
    read each surviving file for its first user message. For a power user
    with thousands of historical sessions this skips ~99% of the I/O.
    """
    sdir = _session_dir_for(project_path)
    if not sdir.is_dir():
        return []
    store = _read_sessions()
    meta_by_id: dict[str, dict[str, Any]] = (
        store.get("by_project", {})
             .get(project_path, {})
             .get("sessions", {})
    ) or {}
    # Pass 1: cheap stat + pin classification.
    candidates: list[tuple[Path, float, int, bool, dict[str, Any]]] = []
    for jsonl in sdir.glob("*.jsonl"):
        try:
            stat = jsonl.stat()
        except OSError:
            continue
        sid = jsonl.stem
        meta = meta_by_id.get(sid, {})
        if meta.get("deleted"):
            continue
        candidates.append((jsonl, stat.st_mtime, stat.st_size, bool(meta.get("pinned")), meta))
    # Sort: pinned first, then newest by mtime.
    candidates.sort(key=lambda r: (-1 if r[3] else 0, -r[1]))
    candidates = candidates[:limit]
    # Pass 2: derive labels only for the survivors.
    out: list[dict[str, Any]] = []
    for jsonl, mtime, size, pinned, meta in candidates:
        label = meta.get("label") or _read_first_user_text(jsonl) or "(empty session)"
        out.append({
            "session_id": jsonl.stem,
            "label": label,
            "pinned": pinned,
            "last_used": mtime,
            "size_bytes": size,
        })
    return out


def read_session_messages(
    project_path: str, session_id: str, *, limit: int = 400
) -> list[dict[str, Any]]:
    """Replay payload for the chat panel — flattened user+assistant turns."""
    if not _is_valid_session_id(session_id):
        return []
    sdir = _session_dir_for(project_path)
    jsonl = sdir / f"{session_id}.jsonl"
    try:
        jsonl.resolve().relative_to(sdir.resolve())
    except (OSError, ValueError):
        return []
    if not jsonl.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with jsonl.open("r", encoding="utf-8") as fh:
            for line in fh:
                if len(out) >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = evt.get("type")
                if kind not in ("user", "assistant"):
                    continue
                msg = evt.get("message") or {}
                content = msg.get("content")
                text_parts: list[str] = []
                if isinstance(content, str):
                    text_parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text" and isinstance(block.get("text"), str):
                            text_parts.append(block["text"])
                text = "".join(text_parts).strip()
                if not text:
                    continue
                out.append({
                    "role": kind,
                    "text": text,
                    "ts": evt.get("timestamp"),
                })
    except OSError:
        return []
    return out


def update_session_metadata(
    project_path: str, session_id: str, *,
    label: str | None = None,
    pinned: bool | None = None,
    deleted: bool | None = None,
) -> None:
    """Patch the per-session metadata block. Creates entries on demand.

    Falsy values are stored as a key *absence* rather than `False` / empty
    string, so the persisted JSON stays minimal and "unset vs. explicitly
    off" doesn't get ambiguous on re-read.
    """
    if not _is_valid_session_id(session_id):
        return
    store = _read_sessions()
    by_project = store.setdefault("by_project", {})
    proj = by_project.setdefault(project_path, {})
    sessions = proj.setdefault("sessions", {})
    entry = sessions.setdefault(session_id, {})
    if label is not None:
        clean = label.strip()[:200]
        if clean:
            entry["label"] = clean
        else:
            entry.pop("label", None)
    if pinned is not None:
        if pinned:
            entry["pinned"] = True
        else:
            entry.pop("pinned", None)
    if deleted is not None:
        if deleted:
            entry["deleted"] = True
        else:
            entry.pop("deleted", None)
    if not entry:
        sessions.pop(session_id, None)
    _write_sessions(store)


_SESSION_ID_CHARS = frozenset("0123456789abcdefABCDEF-")


def _is_valid_session_id(session_id: str) -> bool:
    return bool(session_id) and all(c in _SESSION_ID_CHARS for c in session_id)


# C0 spike findings: see CLAUDE_CLI_NOTES.md. v1 ships with these defaults.
DEFAULT_MODEL = "sonnet"            # Opus cache-creation tax is steep on first turn.
DEFAULT_PERMISSION_MODE = "bypassPermissions"  # Until we see a real approval event.
VALID_PERMISSION_MODES = {
    "default", "acceptEdits", "auto", "bypassPermissions", "dontAsk", "plan",
}


class ChatError(RuntimeError):
    """Raised for unrecoverable session failures (binary missing, etc.)."""


def _claude_binary() -> str:
    explicit = os.environ.get("SFSKILLS_CLAUDE_BIN")
    if explicit:
        return explicit
    resolved = shutil.which("claude")
    if resolved:
        return resolved
    raise ChatError(
        "Claude Code CLI ('claude') was not found on PATH. Install it from "
        "https://docs.claude.com/claude-code or set SFSKILLS_CLAUDE_BIN to "
        "the absolute path of the claude binary."
    )


@dataclass
class ChatSession:
    """One live ``claude`` subprocess. One per active dashboard tab."""

    project_path: Path
    permission_mode: str = DEFAULT_PERMISSION_MODE
    model: str = DEFAULT_MODEL
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    resume: bool = False  # True means use --resume with our session_id
    proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _stdin_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False,
    )
    _stderr_task: asyncio.Task | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.permission_mode not in VALID_PERMISSION_MODES:
            raise ChatError(
                f"unknown permission_mode {self.permission_mode!r}; "
                f"expected one of {sorted(VALID_PERMISSION_MODES)}"
            )
        # Canonicalize the path — resolves symlinks (e.g. macOS
        # /var → /private/var) so init and set_project produce the same
        # session state for the same logical project.
        try:
            self.project_path = self.project_path.expanduser().resolve(strict=True)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ChatError(
                f"project_path is not a directory: {self.project_path} ({exc})"
            )
        if not self.project_path.is_dir():
            raise ChatError(f"project_path is not a directory: {self.project_path}")

    # --------------------------------------------------------------- #
    # Spawn / teardown                                                 #
    # --------------------------------------------------------------- #

    async def spawn(self) -> None:
        """Launch the claude subprocess. Idempotent — second call is a no-op."""
        if self.proc is not None and self.proc.returncode is None:
            return
        binary = _claude_binary()
        # Build the argv. We rely on claude's own session persistence for
        # --resume to find the prior conversation; passing
        # --no-session-persistence makes resume fail because the session
        # is never saved in the first place.
        args = [
            binary,
            "-p",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--permission-mode", self.permission_mode,
            "--model", self.model,
        ]
        if self.resume:
            # --resume implies the session id; don't also pass --session-id
            # since that would conflict with the resumed session's identity.
            args.extend(["--resume", self.session_id])
        else:
            args.extend(["--session-id", self.session_id])
        log.info(
            "chat: spawning claude (cwd=%s, model=%s, mode=%s, session=%s, resume=%s)",
            self.project_path, self.model, self.permission_mode, self.session_id, self.resume,
        )
        self.proc = await asyncio.create_subprocess_exec(  # noqa: S603 — argv form, no shell
            *args,
            cwd=str(self.project_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Capture the most recent stderr so we can surface it to the
        # client if the process dies before we can send messages to it.
        self._stderr_tail: list[str] = []
        # First stdout line gets consumed by wait_for_ready() to detect
        # liveness; we stash it here so iter_events() can re-emit it
        # instead of swallowing the system.init event.
        self._buffered_first_line: bytes | None = None
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def wait_for_ready(self, timeout: float = 8.0) -> bool:
        """Race first stdout line vs subprocess exit.

        Returns True if the subprocess produced a line (healthy) or the
        wait timed out (assumed healthy — claude is slow but still up).
        Returns False if the subprocess exited before producing any
        output, which is how a stale --resume id (and most other fatal
        startup errors) manifest.
        """
        if self.proc is None or self.proc.stdout is None:
            return False
        first = asyncio.create_task(self.proc.stdout.readline())
        exited = asyncio.create_task(self.proc.wait())
        try:
            done, pending = await asyncio.wait(
                [first, exited],
                return_when=asyncio.FIRST_COMPLETED,
                timeout=timeout,
            )
        finally:
            # Cancel + reap the losing task so it doesn't sit pending in
            # the event loop. `gather(return_exceptions=True)` swallows
            # CancelledError and any incidental errors.
            losers = [t for t in (first, exited) if not t.done()]
            for t in losers:
                t.cancel()
            if losers:
                await asyncio.gather(*losers, return_exceptions=True)
        if exited in done and self.proc.returncode is not None:
            return False
        if first in done:
            line = first.result()
            if line:
                self._buffered_first_line = line
        return True

    async def _drain_stderr(self) -> None:
        if self.proc is None or self.proc.stderr is None:
            return
        try:
            while not self.proc.stderr.at_eof():
                line = await self.proc.stderr.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", "replace").rstrip()
                log.debug("claude stderr: %s", decoded)
                # Keep a short tail so send_message() can include real
                # context if the subprocess has already died.
                self._stderr_tail.append(decoded)
                if len(self._stderr_tail) > 20:
                    self._stderr_tail = self._stderr_tail[-20:]
        except asyncio.CancelledError:
            pass

    async def terminate(self) -> None:
        """Graceful shutdown — close stdin first, then escalate.

        Order:
        1. Close stdin (EOF signals claude to wrap up).
        2. Wait 3s for clean exit.
        3. SIGTERM.
        4. Wait 2s.
        5. SIGKILL.
        6. Cancel the stderr-drain task regardless of how the process
           ended (a subprocess that died on its own still has a pending
           readline() in flight).
        """
        proc = self.proc
        stderr_task = self._stderr_task
        # Null these out up front so any concurrent iter_events / send
        # observes "no live process" instead of racing on a half-dead one.
        self.proc = None
        self._stderr_task = None
        if proc is not None and proc.returncode is None:
            # Step 1: close stdin.
            if proc.stdin is not None and not proc.stdin.is_closing():
                try:
                    proc.stdin.close()
                except (OSError, RuntimeError):
                    pass
            # Step 2 + 3 + 4 + 5.
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                log.info("chat: SIGTERM session=%s", self.session_id)
                try:
                    proc.send_signal(signal.SIGTERM)
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except (ProcessLookupError, asyncio.TimeoutError):
                    log.warning("chat: SIGKILL session=%s", self.session_id)
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
        # Step 6: reap the stderr task whether or not the process exited
        # on its own.
        if stderr_task is not None:
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass

    # --------------------------------------------------------------- #
    # I/O                                                              #
    # --------------------------------------------------------------- #

    async def send_message(
        self,
        content: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """Write a user message (text + optional inline images) to claude's stdin.

        Format mirrors the Anthropic API's user message shape; this is
        what ``--input-format stream-json`` expects per Claude Code's
        streaming-input contract.

        ``attachments`` is a list of pre-validated dicts from the
        WebSocket handler, each shaped like::

            {"kind": "image", "media_type": "image/png", "data": "<base64>"}

        and translated here into Anthropic-flavor content blocks. Images
        come first so the model sees them before the prompt that
        references them — same ordering as the Anthropic API examples.
        """
        if self.proc is None or self.proc.stdin is None or self.proc.stdin.is_closing():
            # Pull the last stderr line so the user sees the real cause
            # (e.g. "session 4ee5... not found") rather than a generic
            # "subprocess not running".
            tail = getattr(self, "_stderr_tail", [])
            last = next((line for line in reversed(tail) if line.strip()), "")
            detail = f": {last}" if last else ""
            raise ChatError(f"claude subprocess died{detail}")
        blocks: list[dict[str, Any]] = []
        for att in attachments or []:
            if att.get("kind") != "image":
                continue
            media_type = att.get("media_type")
            data_b64 = att.get("data")
            if not isinstance(media_type, str) or not isinstance(data_b64, str):
                continue
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data_b64,
                },
            })
        if content.strip():
            blocks.append({"type": "text", "text": content})
        if not blocks:
            raise ChatError("user_message must include text or at least one attachment")
        payload = {
            "type": "user",
            "message": {
                "role": "user",
                "content": blocks,
            },
        }
        line = (json.dumps(payload) + "\n").encode("utf-8")
        async with self._stdin_lock:
            self.proc.stdin.write(line)
            await self.proc.stdin.drain()

    async def iter_events(self) -> AsyncIterator[dict[str, Any]]:
        """Async generator yielding parsed JSON events from stdout.

        Stops when the subprocess closes stdout or exits. Malformed JSON
        lines are skipped silently (mirrors the SSE tail in events.py).
        """
        if self.proc is None or self.proc.stdout is None:
            return
        stdout = self.proc.stdout
        # If wait_for_ready() consumed the first line as a liveness
        # check, replay it here so the WebSocket pump still gets it.
        if self._buffered_first_line is not None:
            buffered, self._buffered_first_line = self._buffered_first_line, None
            stripped = buffered.strip()
            if stripped:
                try:
                    yield json.loads(stripped)
                except json.JSONDecodeError:
                    log.debug("chat: skipped malformed buffered line: %s", stripped[:200])
        while not stdout.at_eof():
            try:
                line = await stdout.readline()
            except (asyncio.CancelledError, BrokenPipeError):
                break
            if not line:
                break
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield json.loads(stripped)
            except json.JSONDecodeError:
                log.debug("chat: skipped malformed line: %s", stripped[:200])
                continue


# --------------------------------------------------------------------------- #
# WebSocket handler                                                           #
# --------------------------------------------------------------------------- #


async def _respawn(
    ws,
    *,
    old_session: ChatSession | None,
    old_pump: asyncio.Task | None,
    project_path: Path,
    permission_mode: str | None,
    model: str | None,
    pump_factory,
    session_id: str | None = None,
    resume: bool = False,
) -> tuple[ChatSession | None, asyncio.Task | None]:
    """Tear down the old subprocess and spawn a fresh one with new config.

    Used by ``set_project`` / ``set_permission_mode`` / ``set_model`` —
    plus ``set_session`` for the history picker (passes session_id +
    resume=True). Claude Code's CLI doesn't accept live reconfiguration,
    so every config change forces a subprocess restart.
    """
    if old_pump is not None:
        old_pump.cancel()
        try:
            await old_pump
        except (asyncio.CancelledError, Exception):
            pass
    if old_session is not None:
        try:
            await old_session.terminate()
        except Exception:  # noqa: BLE001
            log.exception("chat: terminate during respawn failed")
    fallback_mode = old_session.permission_mode if old_session else DEFAULT_PERMISSION_MODE
    fallback_model = old_session.model if old_session else DEFAULT_MODEL
    new_session: ChatSession | None = None
    try:
        new_session = ChatSession(
            project_path=project_path,
            permission_mode=permission_mode or fallback_mode,
            model=model or fallback_model,
            session_id=session_id or str(uuid.uuid4()),
            resume=resume,
        )
        await new_session.spawn()
    except ChatError as exc:
        # If we got far enough to create a subprocess before something
        # threw (rare — only on a between-statements failure inside
        # spawn), make sure we don't orphan it.
        if new_session is not None:
            try:
                await new_session.terminate()
            except Exception:  # noqa: BLE001
                log.exception("chat: terminate of partial spawn failed")
        await ws.send_json({"type": "control", "subtype": "error", "error": str(exc)})
        return None, None
    projects_mod.touch_project(str(new_session.project_path))
    save_session(
        str(new_session.project_path),
        new_session.session_id,
        new_session.permission_mode,
        new_session.model,
    )
    new_pump = asyncio.create_task(pump_factory())
    await ws.send_json({
        "type": "control",
        "subtype": "ready",
        "session_id": new_session.session_id,
        "project_path": str(new_session.project_path),
        "permission_mode": new_session.permission_mode,
        "model": new_session.model,
        "resumed": resume,
    })
    return new_session, new_pump


async def ws_chat_handler(request) -> Any:
    """``GET /chat/ws`` — bidirectional chat protocol.

    Wire format:

    Client → server:
        {"type": "init", "project_path": "...", "permission_mode": "...",
         "model": "sonnet", "session_id": "...", "resume": false}
        {"type": "user_message", "content": "..."}
        {"type": "stop"}

    Server → client:
        Every event from the claude subprocess (system / stream_event /
        assistant / result) is forwarded verbatim, plus our own:
        {"type": "control", "subtype": "ready", "session_id": "..."}
        {"type": "control", "subtype": "error", "error": "..."}
        {"type": "control", "subtype": "closed", "reason": "..."}

    Subprocess lifecycle is bound to the WebSocket: connect ⇒ no
    subprocess yet, init ⇒ spawn, disconnect ⇒ terminate.
    """
    from aiohttp import web

    # max_msg_size aligns with the attachments cap (14MB base64 +
    # text content + JSON envelope overhead). Default 4MB would reject
    # any user_message with an image as a 1009 close before our
    # validators run.
    ws = web.WebSocketResponse(heartbeat=30.0, autoping=True, max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)

    session: ChatSession | None = None
    pump_task: asyncio.Task | None = None

    async def pump_events() -> None:
        """Forward subprocess events to the WebSocket client."""
        if session is None:
            return
        try:
            async for event in session.iter_events():
                if ws.closed:
                    break
                await ws.send_json(event)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 — we want telemetry on every failure
            log.exception("chat: pump failed")
            # Don't try to send a control message if the socket is already
            # closed — the close was almost certainly the cause of the
            # exception, and aiohttp raises RuntimeError on send-after-close.
            if not ws.closed:
                try:
                    await ws.send_json({
                        "type": "control",
                        "subtype": "error",
                        "error": f"event pump failed: {exc!s}",
                    })
                except Exception:
                    pass

    try:
        async for msg in ws:
            if msg.type != msg.type.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                await ws.send_json({
                    "type": "control", "subtype": "error",
                    "error": "non-JSON message",
                })
                continue

            kind = data.get("type")
            if kind == "init":
                if session is not None:
                    await ws.send_json({
                        "type": "control", "subtype": "error",
                        "error": "session already initialized; reconnect to start a new one",
                    })
                    continue
                # Resume policy: opt-out, not opt-in. A page reload should
                # land the user back in their previous conversation; the
                # frontend explicitly passes resume=false from the "New
                # chat" button to start fresh.
                want_resume = data.get("resume", True) is not False
                requested_path = Path(data.get("project_path") or ".")
                try:
                    resolved_path = requested_path.expanduser().resolve(strict=True)
                except (OSError, ValueError, RuntimeError):
                    await ws.send_json({
                        "type": "control", "subtype": "error",
                        "error": f"project path not found: {requested_path}",
                    })
                    continue
                resume_session_id: str | None = None
                if want_resume and not data.get("session_id"):
                    saved = get_last_session(str(resolved_path))
                    if saved and saved.get("session_id"):
                        resume_session_id = str(saved["session_id"])
                effective_session_id = (
                    data.get("session_id")
                    or resume_session_id
                    or str(uuid.uuid4())
                )
                effective_resume = bool(
                    data.get("resume") or resume_session_id
                )
                recovered_from_stale = False
                try:
                    session = ChatSession(
                        project_path=resolved_path,
                        permission_mode=data.get("permission_mode") or DEFAULT_PERMISSION_MODE,
                        model=data.get("model") or DEFAULT_MODEL,
                        session_id=effective_session_id,
                        resume=effective_resume,
                    )
                    await session.spawn()
                    # Wait for either the first event or the subprocess
                    # to exit. A stale session id (history rotated, claude
                    # reinstalled, sessions cleared) makes the subprocess
                    # exit before producing any output; so does a bad
                    # binary, bad model, or other startup failure.
                    healthy = await session.wait_for_ready(timeout=8.0)
                    if not healthy:
                        if effective_resume:
                            # Stale resume — recover transparently by
                            # starting fresh instead of leaving the user
                            # stuck in an error loop.
                            log.warning(
                                "chat: resume of %s failed; starting fresh session",
                                effective_session_id,
                            )
                            await session.terminate()
                            session = ChatSession(
                                project_path=resolved_path,
                                permission_mode=data.get("permission_mode") or DEFAULT_PERMISSION_MODE,
                                model=data.get("model") or DEFAULT_MODEL,
                                session_id=str(uuid.uuid4()),
                                resume=False,
                            )
                            await session.spawn()
                            healthy = await session.wait_for_ready(timeout=8.0)
                            if healthy:
                                recovered_from_stale = True
                                resume_session_id = None
                        if not healthy:
                            # Fresh-spawn failure or post-recovery failure
                            # — surface the stderr so the user can act on it.
                            tail = getattr(session, "_stderr_tail", [])
                            last = next((ln for ln in reversed(tail) if ln.strip()), "")
                            detail = f": {last}" if last else ""
                            await session.terminate()
                            raise ChatError(f"claude failed to start{detail}")
                except ChatError as exc:
                    await ws.send_json({
                        "type": "control", "subtype": "error", "error": str(exc),
                    })
                    session = None
                    continue
                projects_mod.touch_project(str(session.project_path))
                save_session(
                    str(session.project_path),
                    session.session_id,
                    session.permission_mode,
                    session.model,
                )
                pump_task = asyncio.create_task(pump_events())
                await ws.send_json({
                    "type": "control", "subtype": "ready",
                    "session_id": session.session_id,
                    "project_path": str(session.project_path),
                    "permission_mode": session.permission_mode,
                    "model": session.model,
                    "resumed": bool(resume_session_id),
                    "stale_resume_recovered": recovered_from_stale,
                })

            elif kind == "user_message":
                if session is None:
                    await ws.send_json({
                        "type": "control", "subtype": "error",
                        "error": "session not initialized; send 'init' first",
                    })
                    continue
                content = data.get("content")
                if not isinstance(content, str):
                    content = ""
                # Bound the text payload to keep the stdin pipe and any
                # downstream model context budget from being blown up by
                # a runaway paste. 256KB is roughly 60K tokens — plenty
                # for a normal turn, well under any practical limit.
                MAX_CONTENT_BYTES = 256 * 1024
                if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
                    await ws.send_json({
                        "type": "control", "subtype": "error",
                        "error": f"message too long (max {MAX_CONTENT_BYTES // 1024}KB)",
                    })
                    continue
                raw_attachments = data.get("attachments") or []
                if not isinstance(raw_attachments, list):
                    raw_attachments = []
                # Validate each attachment. We trust the frontend has
                # already capped sizes, but enforce limits server-side
                # too: stdin pipes have OS-level buffer limits and the
                # Anthropic API rejects oversized image payloads.
                ALLOWED_IMAGE_TYPES = {
                    "image/png", "image/jpeg", "image/gif", "image/webp",
                }
                MAX_IMAGE_BYTES_BASE64 = 7_000_000   # ~5.2 MB raw per image
                MAX_TOTAL_BYTES_BASE64 = 14_000_000  # backstop for the whole message
                attachments: list[dict[str, Any]] = []
                total = 0
                for att in raw_attachments:
                    if not isinstance(att, dict):
                        continue
                    if att.get("kind") != "image":
                        continue
                    media = att.get("media_type")
                    data_b64 = att.get("data")
                    if media not in ALLOWED_IMAGE_TYPES:
                        continue
                    if not isinstance(data_b64, str):
                        continue
                    size = len(data_b64)
                    if size > MAX_IMAGE_BYTES_BASE64:
                        await ws.send_json({
                            "type": "control", "subtype": "error",
                            "error": f"image too large (>{MAX_IMAGE_BYTES_BASE64 // 1_000_000}MB base64)",
                        })
                        attachments = []
                        break
                    if total + size > MAX_TOTAL_BYTES_BASE64:
                        await ws.send_json({
                            "type": "control", "subtype": "error",
                            "error": "attachments exceed total size cap",
                        })
                        attachments = []
                        break
                    total += size
                    attachments.append({"kind": "image", "media_type": media, "data": data_b64})
                if not content.strip() and not attachments:
                    await ws.send_json({
                        "type": "control", "subtype": "error",
                        "error": "'content' must be a non-empty string or attachments must be provided",
                    })
                    continue
                try:
                    await session.send_message(content, attachments=attachments)
                except ChatError as exc:
                    await ws.send_json({
                        "type": "control", "subtype": "error", "error": str(exc),
                    })

            elif kind == "set_project":
                # Switch projects — terminate current subprocess, re-spawn
                # with new cwd. Permission mode and model carry over unless
                # the client explicitly overrides them in the message.
                new_path = data.get("path")
                if not new_path:
                    await ws.send_json({
                        "type": "control", "subtype": "error",
                        "error": "'path' is required",
                    })
                    continue
                try:
                    project_path = Path(new_path).expanduser().resolve(strict=True)
                except (OSError, ValueError, RuntimeError):
                    await ws.send_json({
                        "type": "control", "subtype": "error",
                        "error": f"path is not an existing directory: {new_path}",
                    })
                    continue
                session, pump_task = await _respawn(
                    ws,
                    old_session=session,
                    old_pump=pump_task,
                    project_path=project_path,
                    permission_mode=data.get("permission_mode"),
                    model=data.get("model"),
                    pump_factory=pump_events,
                )

            elif kind == "set_permission_mode":
                # Mode change forces a subprocess restart — claude doesn't
                # accept a live permission-mode change. Conversation context
                # is lost; v1 trade-off until C6's session resumption.
                if session is None:
                    await ws.send_json({
                        "type": "control", "subtype": "error",
                        "error": "session not initialized; send 'init' first",
                    })
                    continue
                mode = data.get("mode")
                if mode not in VALID_PERMISSION_MODES:
                    await ws.send_json({
                        "type": "control", "subtype": "error",
                        "error": (
                            f"unknown permission mode {mode!r}; expected one of "
                            f"{sorted(VALID_PERMISSION_MODES)}"
                        ),
                    })
                    continue
                session, pump_task = await _respawn(
                    ws,
                    old_session=session,
                    old_pump=pump_task,
                    project_path=session.project_path,
                    permission_mode=mode,
                    model=session.model,
                    pump_factory=pump_events,
                )

            elif kind == "set_model":
                # Same restart pattern as permission mode.
                if session is None:
                    await ws.send_json({
                        "type": "control", "subtype": "error",
                        "error": "session not initialized; send 'init' first",
                    })
                    continue
                model = data.get("model")
                if not isinstance(model, str) or not model.strip():
                    await ws.send_json({
                        "type": "control", "subtype": "error",
                        "error": "'model' must be a non-empty string (e.g. 'sonnet')",
                    })
                    continue
                session, pump_task = await _respawn(
                    ws,
                    old_session=session,
                    old_pump=pump_task,
                    project_path=session.project_path,
                    permission_mode=session.permission_mode,
                    model=model.strip(),
                    pump_factory=pump_events,
                )

            elif kind == "set_session":
                # Switch to a specific historical session — used by the
                # history picker. Resume the requested session_id, keep
                # everything else (project, mode, model) where it is.
                if session is None:
                    await ws.send_json({
                        "type": "control", "subtype": "error",
                        "error": "session not initialized; send 'init' first",
                    })
                    continue
                new_session_id = data.get("session_id")
                if not isinstance(new_session_id, str) or not new_session_id.strip():
                    await ws.send_json({
                        "type": "control", "subtype": "error",
                        "error": "'session_id' must be a non-empty string",
                    })
                    continue
                session, pump_task = await _respawn(
                    ws,
                    old_session=session,
                    old_pump=pump_task,
                    project_path=session.project_path,
                    permission_mode=session.permission_mode,
                    model=session.model,
                    pump_factory=pump_events,
                    session_id=new_session_id.strip(),
                    resume=True,
                )

            elif kind == "stop":
                break

            else:
                await ws.send_json({
                    "type": "control", "subtype": "error",
                    "error": f"unknown message type: {kind!r}",
                })
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        if pump_task is not None:
            pump_task.cancel()
            try:
                await pump_task
            except (asyncio.CancelledError, Exception):
                pass
        if session is not None:
            try:
                await session.terminate()
            except Exception:  # noqa: BLE001
                log.exception("chat: terminate failed")
        if not ws.closed:
            try:
                await ws.send_json({
                    "type": "control", "subtype": "closed", "reason": "disconnect",
                })
            except Exception:
                pass
            await ws.close()

    return ws
