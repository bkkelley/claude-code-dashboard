"""Tests for the chat subprocess manager + WebSocket pass-through (Phase C1).

A Python shim is written to a temp file and pointed at via
``SFSKILLS_CLAUDE_BIN``. The shim mimics a slice of Claude Code's
``--output-format stream-json`` behaviour:

- Emits a ``system/init`` event on startup with the session id taken
  from ``--session-id``.
- Reads JSON-lines from stdin. For every ``{"type": "user"}`` message,
  echoes back a tiny synthetic event sequence (a ``stream_event``
  text_delta and a ``result``) so the round-trip is observable.
- Exits cleanly on EOF.

The shim is the unit-test equivalent of running the real CLI without
actually burning Anthropic tokens.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

try:
    from aiohttp.test_utils import AioHTTPTestCase
    from dashboard import chat as chat_mod
    from dashboard import projects as projects_mod
    from dashboard.app import create_app
except ImportError as exc:
    raise unittest.SkipTest(
        f"dashboard or aiohttp not importable: {exc}. Run "
        "`pip install aiohttp aiohttp_jinja2 jinja2`."
    )


# --------------------------------------------------------------------------- #
# Stub claude binary                                                          #
# --------------------------------------------------------------------------- #


_STUB_SCRIPT = r"""#!__PYTHON__
import argparse
import json
import sys

p = argparse.ArgumentParser()
p.add_argument("-p", "--print", action="store_true")
p.add_argument("--output-format", default="text")
p.add_argument("--input-format", default="text")
p.add_argument("--include-partial-messages", action="store_true")
p.add_argument("--verbose", action="store_true")
p.add_argument("--no-session-persistence", action="store_true")
p.add_argument("--permission-mode", default="default")
p.add_argument("--model", default="sonnet")
p.add_argument("--session-id")
p.add_argument("--resume", default=None)
p.add_argument("prompt", nargs="?")
args, _ = p.parse_known_args()


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


emit({
    "type": "system",
    "subtype": "init",
    "session_id": args.session_id or "stub-session",
    "model": args.model,
    "permissionMode": args.permission_mode,
    "tools": ["Read", "Bash"],
    "mcp_servers": [],
})

# Read user messages from stdin and emit synthetic responses per message.
for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    if msg.get("type") != "user":
        continue
    # Walk the content[0].text path; tolerate either string or list shape.
    text = ""
    content = (msg.get("message") or {}).get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        text = content[0].get("text", "")
    elif isinstance(content, str):
        text = content
    # Echo back: one text_delta event and one result event.
    emit({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": f"echo: {text}"},
        },
        "session_id": args.session_id or "stub-session",
    })
    emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 1,
        "total_cost_usd": 0.0,
        "session_id": args.session_id or "stub-session",
    })
"""


def _write_stub(target: Path) -> None:
    # Use plain .replace() not .format() — the JSON {/} braces in the
    # script body would be interpreted as format placeholders otherwise.
    target.write_text(
        _STUB_SCRIPT.replace("__PYTHON__", sys.executable),
        encoding="utf-8",
    )
    target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# --------------------------------------------------------------------------- #
# ChatSession unit tests                                                      #
# --------------------------------------------------------------------------- #


class ChatSessionFixture(unittest.IsolatedAsyncioTestCase):
    """Base case that points ``SFSKILLS_CLAUDE_BIN`` at the stub."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.stub = self.tmp_path / "claude_stub"
        _write_stub(self.stub)
        self._prior_bin = os.environ.get("SFSKILLS_CLAUDE_BIN")
        os.environ["SFSKILLS_CLAUDE_BIN"] = str(self.stub)
        # Redirect the projects store so init's touch_project doesn't
        # scribble in ~/.claude/ka-sfskills/projects.json.
        self._orig_store = projects_mod.STORE_PATH
        projects_mod.STORE_PATH = self.tmp_path / "projects.json"
        # Project root for the session: also the temp dir.
        self.project = self.tmp_path

    async def asyncTearDown(self) -> None:
        projects_mod.STORE_PATH = self._orig_store
        if self._prior_bin is None:
            os.environ.pop("SFSKILLS_CLAUDE_BIN", None)
        else:
            os.environ["SFSKILLS_CLAUDE_BIN"] = self._prior_bin
        self.tmp.cleanup()


class TestChatSession(ChatSessionFixture):
    async def test_spawn_emits_init_event(self) -> None:
        session = chat_mod.ChatSession(project_path=self.project)
        await session.spawn()
        try:
            # Collect events until result or timeout.
            events = []
            async def collect():
                async for evt in session.iter_events():
                    events.append(evt)
                    if evt.get("type") == "system" and evt.get("subtype") == "init":
                        return
            await asyncio.wait_for(collect(), timeout=5.0)
            self.assertTrue(events, "expected at least one event")
            self.assertEqual(events[0]["type"], "system")
            self.assertEqual(events[0]["subtype"], "init")
            self.assertEqual(events[0]["session_id"], session.session_id)
        finally:
            await session.terminate()

    async def test_send_message_round_trips(self) -> None:
        session = chat_mod.ChatSession(project_path=self.project)
        await session.spawn()
        try:
            received = []
            done = asyncio.Event()

            async def pump():
                async for evt in session.iter_events():
                    received.append(evt)
                    if evt.get("type") == "result":
                        done.set()
                        return

            pump_task = asyncio.create_task(pump())
            # Wait for init.
            for _ in range(50):
                if any(e.get("type") == "system" for e in received):
                    break
                await asyncio.sleep(0.05)
            await session.send_message("hello stub")
            await asyncio.wait_for(done.wait(), timeout=5.0)
            pump_task.cancel()
            with self.subTest("echo present"):
                deltas = [
                    e for e in received
                    if e.get("type") == "stream_event"
                    and e.get("event", {}).get("type") == "content_block_delta"
                ]
                self.assertTrue(deltas, "expected a stream_event delta")
                self.assertEqual(
                    deltas[0]["event"]["delta"]["text"],
                    "echo: hello stub",
                )
        finally:
            await session.terminate()

    async def test_terminate_closes_subprocess(self) -> None:
        session = chat_mod.ChatSession(project_path=self.project)
        await session.spawn()
        proc = session.proc
        self.assertIsNotNone(proc)
        await session.terminate()
        # After terminate, proc reference cleared and the process is gone.
        self.assertIsNone(session.proc)
        # Original proc handle should have a return code now.
        self.assertIsNotNone(proc.returncode)

    async def test_invalid_permission_mode_rejected(self) -> None:
        with self.assertRaises(chat_mod.ChatError):
            chat_mod.ChatSession(
                project_path=self.project,
                permission_mode="invalid-mode",
            )

    async def test_nonexistent_project_rejected(self) -> None:
        with self.assertRaises(chat_mod.ChatError):
            chat_mod.ChatSession(project_path=Path("/this/path/does/not/exist/abc123"))

    async def test_missing_binary_surfaces_clearly(self) -> None:
        # Point at a path that doesn't exist; spawn() raises ChatError.
        os.environ["SFSKILLS_CLAUDE_BIN"] = str(self.tmp_path / "definitely-not-there")
        session = chat_mod.ChatSession(project_path=self.project)
        # The binary check happens inside spawn(); construction succeeds
        # because we don't verify the binary at __post_init__ time.
        with self.assertRaises(Exception):
            await session.spawn()


# --------------------------------------------------------------------------- #
# WebSocket pass-through tests                                                #
# --------------------------------------------------------------------------- #


class WSChatFixture(AioHTTPTestCase):
    """End-to-end: real WebSocket client talking to the real handler.

    Uses the stub binary just like the unit tests above. Also redirects
    the projects store so the WS handler's touch_project doesn't scribble
    in the developer's real ~/.claude/ka-sfskills/projects.json.
    """

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.stub = self.tmp_path / "claude_stub"
        _write_stub(self.stub)
        self._prior_bin = os.environ.get("SFSKILLS_CLAUDE_BIN")
        os.environ["SFSKILLS_CLAUDE_BIN"] = str(self.stub)
        self._orig_store = projects_mod.STORE_PATH
        projects_mod.STORE_PATH = self.tmp_path / "projects.json"
        # Redirect chat-sessions store too — the WS handler now writes
        # last_session per project on every ready event.
        self._orig_sessions = chat_mod.SESSIONS_PATH
        chat_mod.SESSIONS_PATH = self.tmp_path / "chat-sessions.json"
        await super().asyncSetUp()

    async def asyncTearDown(self) -> None:
        await super().asyncTearDown()
        chat_mod.SESSIONS_PATH = self._orig_sessions
        projects_mod.STORE_PATH = self._orig_store
        if self._prior_bin is None:
            os.environ.pop("SFSKILLS_CLAUDE_BIN", None)
        else:
            os.environ["SFSKILLS_CLAUDE_BIN"] = self._prior_bin
        self.tmp.cleanup()

    async def get_application(self):
        return create_app()


class TestWSChat(WSChatFixture):
    async def test_full_chat_round_trip(self) -> None:
        async with self.client.ws_connect(
            "/chat/ws", headers={"Host": "127.0.0.1"}
        ) as ws:
            await ws.send_json({
                "type": "init",
                "project_path": str(self.tmp_path),
                "permission_mode": "bypassPermissions",
                "model": "sonnet",
            })

            received: list[dict] = []
            saw_ready = asyncio.Event()
            saw_result = asyncio.Event()

            async def collect():
                async for msg in ws:
                    if msg.type == msg.type.TEXT:
                        data = json.loads(msg.data)
                        received.append(data)
                        if data.get("type") == "control" and data.get("subtype") == "ready":
                            saw_ready.set()
                        if data.get("type") == "result":
                            saw_result.set()
                            return

            collector = asyncio.create_task(collect())
            await asyncio.wait_for(saw_ready.wait(), timeout=5.0)
            await ws.send_json({"type": "user_message", "content": "hi"})
            await asyncio.wait_for(saw_result.wait(), timeout=5.0)
            collector.cancel()
            try:
                await collector
            except asyncio.CancelledError:
                pass
            # Verify the synthetic delta arrived through the WebSocket.
            deltas = [
                e for e in received
                if e.get("type") == "stream_event"
                and e.get("event", {}).get("type") == "content_block_delta"
            ]
            self.assertTrue(deltas)
            self.assertIn("echo: hi", deltas[0]["event"]["delta"]["text"])

    async def test_user_message_before_init_returns_error(self) -> None:
        async with self.client.ws_connect(
            "/chat/ws", headers={"Host": "127.0.0.1"}
        ) as ws:
            await ws.send_json({"type": "user_message", "content": "hi"})
            msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
            data = json.loads(msg.data)
            self.assertEqual(data["type"], "control")
            self.assertEqual(data["subtype"], "error")

    async def test_unknown_message_type_returns_error(self) -> None:
        async with self.client.ws_connect(
            "/chat/ws", headers={"Host": "127.0.0.1"}
        ) as ws:
            await ws.send_json({"type": "wat"})
            msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
            data = json.loads(msg.data)
            self.assertEqual(data["type"], "control")
            self.assertEqual(data["subtype"], "error")

    async def test_non_json_message_returns_error(self) -> None:
        async with self.client.ws_connect(
            "/chat/ws", headers={"Host": "127.0.0.1"}
        ) as ws:
            await ws.send_str("not json at all")
            msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
            data = json.loads(msg.data)
            self.assertEqual(data["type"], "control")
            self.assertEqual(data["subtype"], "error")

    async def test_double_init_rejected(self) -> None:
        async with self.client.ws_connect(
            "/chat/ws", headers={"Host": "127.0.0.1"}
        ) as ws:
            await ws.send_json({
                "type": "init",
                "project_path": str(self.tmp_path),
            })
            # First ready.
            for _ in range(50):
                msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                data = json.loads(msg.data)
                if data.get("subtype") == "ready":
                    break
            # Second init should be rejected.
            await ws.send_json({
                "type": "init",
                "project_path": str(self.tmp_path),
            })
            # Drain until we see the rejection — there may be subprocess
            # events interleaved.
            saw_error = False
            for _ in range(20):
                msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                data = json.loads(msg.data)
                if data.get("type") == "control" and data.get("subtype") == "error":
                    saw_error = True
                    break
            self.assertTrue(saw_error, "expected error on second init")

    async def test_m2_host_guard_applies_to_websocket(self) -> None:
        # M2 middleware must reject WS upgrade requests from non-allowlisted hosts.
        from aiohttp import ClientError
        with self.assertRaises((ClientError, Exception)):
            async with self.client.ws_connect(
                "/chat/ws",
                headers={"Host": "evil.example.com"},
            ):
                pass

    async def test_stop_message_closes_session(self) -> None:
        async with self.client.ws_connect(
            "/chat/ws", headers={"Host": "127.0.0.1"}
        ) as ws:
            await ws.send_json({
                "type": "init",
                "project_path": str(self.tmp_path),
            })
            # Wait for ready.
            for _ in range(50):
                msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                data = json.loads(msg.data)
                if data.get("subtype") == "ready":
                    break
            await ws.send_json({"type": "stop"})
            # Server should send a "closed" control message and close the WS.
            saw_closed = False
            for _ in range(20):
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                except asyncio.TimeoutError:
                    break
                if msg.type == msg.type.CLOSED:
                    saw_closed = True
                    break
                try:
                    data = json.loads(msg.data)
                except (json.JSONDecodeError, TypeError):
                    continue
                if data.get("type") == "control" and data.get("subtype") == "closed":
                    saw_closed = True
                    break
            self.assertTrue(saw_closed, "expected closed control event or CLOSED frame")


# --------------------------------------------------------------------------- #
# Session persistence (Phase C6)                                              #
# --------------------------------------------------------------------------- #


class TestSessionPersistence(WSChatFixture):
    """A second ``init`` for the same project resumes the prior session."""

    async def test_ready_records_session_then_reuses_on_next_init(self) -> None:
        # First connection — session is freshly minted, persisted on ready.
        async with self.client.ws_connect(
            "/chat/ws", headers={"Host": "127.0.0.1"}
        ) as ws:
            await ws.send_json({
                "type": "init",
                "project_path": str(self.tmp_path),
                "permission_mode": "bypassPermissions",
                "model": "sonnet",
            })
            first_ready = None
            for _ in range(40):
                msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                if msg.type != msg.type.TEXT:
                    continue
                data = json.loads(msg.data)
                if data.get("type") == "control" and data.get("subtype") == "ready":
                    first_ready = data
                    break
            self.assertIsNotNone(first_ready)
            self.assertFalse(first_ready.get("resumed"), "first ready must not be a resume")
            stored_id = first_ready["session_id"]

        # Second connection — same project, no explicit session_id. Backend
        # should look up the persisted one and pass --resume.
        async with self.client.ws_connect(
            "/chat/ws", headers={"Host": "127.0.0.1"}
        ) as ws:
            await ws.send_json({
                "type": "init",
                "project_path": str(self.tmp_path),
                "permission_mode": "bypassPermissions",
                "model": "sonnet",
            })
            second_ready = None
            for _ in range(40):
                msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                if msg.type != msg.type.TEXT:
                    continue
                data = json.loads(msg.data)
                if data.get("type") == "control" and data.get("subtype") == "ready":
                    second_ready = data
                    break
            self.assertIsNotNone(second_ready)
            self.assertTrue(second_ready.get("resumed"), "second ready must report resume")
            self.assertEqual(second_ready["session_id"], stored_id)

    async def test_resume_false_starts_fresh_session(self) -> None:
        """`resume:false` (sent by the 'New chat' button) bypasses persistence."""
        # First init — store something for this project.
        async with self.client.ws_connect(
            "/chat/ws", headers={"Host": "127.0.0.1"}
        ) as ws:
            await ws.send_json({
                "type": "init",
                "project_path": str(self.tmp_path),
            })
            first_id = None
            for _ in range(40):
                msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                data = json.loads(msg.data)
                if data.get("subtype") == "ready":
                    first_id = data["session_id"]
                    break
            self.assertIsNotNone(first_id)
        # Second init with resume:false — backend must NOT reuse.
        async with self.client.ws_connect(
            "/chat/ws", headers={"Host": "127.0.0.1"}
        ) as ws:
            await ws.send_json({
                "type": "init",
                "project_path": str(self.tmp_path),
                "resume": False,
            })
            second = None
            for _ in range(40):
                msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                data = json.loads(msg.data)
                if data.get("subtype") == "ready":
                    second = data
                    break
            self.assertIsNotNone(second)
            self.assertFalse(second.get("resumed"))
            self.assertNotEqual(second["session_id"], first_id)


class TestSessionStoreDirect(unittest.TestCase):
    """Pure-function tests on get_last_session / save_session."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._orig = chat_mod.SESSIONS_PATH
        chat_mod.SESSIONS_PATH = self.tmp_path / "chat-sessions.json"

    def tearDown(self) -> None:
        chat_mod.SESSIONS_PATH = self._orig
        self.tmp.cleanup()

    def test_get_with_no_store_returns_none(self) -> None:
        self.assertIsNone(chat_mod.get_last_session("/any/path"))

    def test_save_then_get_roundtrips(self) -> None:
        chat_mod.save_session(
            "/path/a",
            session_id="abc-123",
            mode="bypassPermissions",
            model="sonnet",
        )
        out = chat_mod.get_last_session("/path/a")
        self.assertIsNotNone(out)
        self.assertEqual(out["session_id"], "abc-123")
        self.assertEqual(out["mode"], "bypassPermissions")
        self.assertEqual(out["model"], "sonnet")
        self.assertIn("last_used", out)

    def test_save_overwrites_previous(self) -> None:
        chat_mod.save_session("/p", "id1", "default", "sonnet")
        chat_mod.save_session("/p", "id2", "plan", "opus")
        out = chat_mod.get_last_session("/p")
        self.assertEqual(out["session_id"], "id2")
        self.assertEqual(out["mode"], "plan")
        self.assertEqual(out["model"], "opus")

    def test_corrupt_store_treated_as_empty(self) -> None:
        chat_mod.SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        chat_mod.SESSIONS_PATH.write_text("{ broken", encoding="utf-8")
        self.assertIsNone(chat_mod.get_last_session("/p"))
        # And save still works after the bad file.
        chat_mod.save_session("/p", "id", "default", "sonnet")
        self.assertEqual(chat_mod.get_last_session("/p")["session_id"], "id")

    def test_save_session_preserves_sessions_submap(self) -> None:
        """save_session must merge, not replace — otherwise it wipes the
        per-session metadata (labels, pins) written by
        update_session_metadata. This was a real data-loss bug.
        """
        chat_mod.save_session("/p", "sid-1", "bypassPermissions", "sonnet")
        chat_mod.update_session_metadata(
            "/p", "abcd1234-ef56-7890-abcd-ef1234567890",
            label="Pinned conversation", pinned=True,
        )
        # New ready event for a different session.
        chat_mod.save_session("/p", "sid-2", "default", "opus")
        store = chat_mod._read_sessions()
        proj = store["by_project"]["/p"]
        self.assertEqual(proj["session_id"], "sid-2")
        self.assertIn("sessions", proj, "metadata sub-map clobbered by save_session")
        self.assertIn("abcd1234-ef56-7890-abcd-ef1234567890", proj["sessions"])
        self.assertEqual(
            proj["sessions"]["abcd1234-ef56-7890-abcd-ef1234567890"]["label"],
            "Pinned conversation",
        )


class TestSessionTranscripts(unittest.TestCase):
    """list_sessions / read_session_messages / update_session_metadata."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._orig_projects = chat_mod.CLAUDE_PROJECTS_DIR
        self._orig_sessions = chat_mod.SESSIONS_PATH
        chat_mod.CLAUDE_PROJECTS_DIR = self.tmp_path / "projects"
        chat_mod.SESSIONS_PATH = self.tmp_path / "chat-sessions.json"
        # Set up one project with three transcripts.
        self.project_path = "/Users/test/proj"
        self.sdir = chat_mod._session_dir_for(self.project_path)
        self.sdir.mkdir(parents=True)
        self._write_transcript(
            "11111111-1111-1111-1111-111111111111",
            [{"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hello first"}]}}],
        )
        self._write_transcript(
            "22222222-2222-2222-2222-222222222222",
            [
                {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "second hi"}]}},
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hey back"}]}},
            ],
        )
        # Third with no user message at all — will get the "(empty session)" label.
        self._write_transcript("33333333-3333-3333-3333-333333333333", [{"type": "queue-operation"}])

    def tearDown(self) -> None:
        chat_mod.CLAUDE_PROJECTS_DIR = self._orig_projects
        chat_mod.SESSIONS_PATH = self._orig_sessions
        self.tmp.cleanup()

    def _write_transcript(self, sid: str, events: list) -> None:
        p = self.sdir / f"{sid}.jsonl"
        with p.open("w", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e) + "\n")

    def test_list_sessions_returns_empty_for_unknown_project(self) -> None:
        self.assertEqual(chat_mod.list_sessions("/no/such/path"), [])

    def test_list_sessions_includes_all_transcripts(self) -> None:
        out = chat_mod.list_sessions(self.project_path)
        ids = {r["session_id"] for r in out}
        self.assertEqual(len(ids), 3)
        labels = {r["session_id"]: r["label"] for r in out}
        self.assertEqual(labels["11111111-1111-1111-1111-111111111111"], "hello first")
        self.assertEqual(labels["22222222-2222-2222-2222-222222222222"], "second hi")
        self.assertEqual(labels["33333333-3333-3333-3333-333333333333"], "(empty session)")

    def test_list_sessions_skips_deleted_and_honors_pinned(self) -> None:
        chat_mod.update_session_metadata(
            self.project_path, "33333333-3333-3333-3333-333333333333", deleted=True,
        )
        chat_mod.update_session_metadata(
            self.project_path, "11111111-1111-1111-1111-111111111111", pinned=True,
        )
        out = chat_mod.list_sessions(self.project_path)
        ids = [r["session_id"] for r in out]
        self.assertNotIn("33333333-3333-3333-3333-333333333333", ids)
        self.assertEqual(ids[0], "11111111-1111-1111-1111-111111111111",
                         "pinned session should sort first")

    def test_list_sessions_honors_custom_label(self) -> None:
        chat_mod.update_session_metadata(
            self.project_path, "22222222-2222-2222-2222-222222222222",
            label="My favorite chat",
        )
        out = chat_mod.list_sessions(self.project_path)
        labels = {r["session_id"]: r["label"] for r in out}
        self.assertEqual(labels["22222222-2222-2222-2222-222222222222"], "My favorite chat")

    def test_read_session_messages_extracts_user_assistant_text(self) -> None:
        msgs = chat_mod.read_session_messages(
            self.project_path, "22222222-2222-2222-2222-222222222222",
        )
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["text"], "second hi")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["text"], "hey back")

    def test_read_session_messages_rejects_path_traversal(self) -> None:
        # Plant a sensitive file outside the project's session dir so
        # we can prove the traversal didn't read it.
        outside = self.tmp_path / "secret.jsonl"
        outside.write_text(
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": "SHOULD NOT LEAK"}]},
            }) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            chat_mod.read_session_messages(self.project_path, "../../../secret"),
            [],
        )
        self.assertEqual(
            chat_mod.read_session_messages(self.project_path, "foo/bar"),
            [],
        )

    def test_update_session_metadata_rejects_invalid_session_id(self) -> None:
        # Should not write anything to the store.
        chat_mod.update_session_metadata(
            self.project_path, "../../../etc/passwd", label="evil",
        )
        store = chat_mod._read_sessions()
        sessions = store.get("by_project", {}).get(self.project_path, {}).get("sessions", {})
        self.assertNotIn("../../../etc/passwd", sessions)

    def test_update_session_metadata_pops_falsy_values(self) -> None:
        sid = "44444444-4444-4444-4444-444444444444"
        chat_mod.update_session_metadata(self.project_path, sid, label="hello", pinned=True)
        chat_mod.update_session_metadata(self.project_path, sid, label="", pinned=False)
        store = chat_mod._read_sessions()
        # All metadata cleared → entry pruned entirely.
        sessions = store["by_project"][self.project_path].get("sessions", {})
        self.assertNotIn(sid, sessions, "fully-cleared metadata should drop the entry")


if __name__ == "__main__":
    unittest.main()
