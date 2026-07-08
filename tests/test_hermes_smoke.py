"""Smoke tests for Hermes mode wiring (Phase 2).

These mock the `hermes acp` subprocess entirely, so they run without WSL, without
a Hermes install, and without llama-server. They guard the two things most likely to
break silently: (1) the ACP frame -> Loom event translation in
``hermes_client.run_hermes``, and (2) the v1 deadlock — a slow permission
round-trip must NOT stall the stdout read loop.

DB-side: ``mode='hermes'`` must persist (no schema migration needed — the
``mode`` column has no CHECK constraint).
"""

import asyncio
import json

import pytest

import database as db
import hermes_client


@pytest.fixture(autouse=True)
async def _clean_hermes_runtimes():
    await hermes_client.shutdown_hermes_runtimes()
    yield
    await hermes_client.shutdown_hermes_runtimes()


# --------------------------------------------------------------------------- #
# Fake asyncio subprocess
# --------------------------------------------------------------------------- #

class _FakeStreamWriter:
    """Stand-in for asyncio.StreamWriter on the child's stdin. Records writes
    and (optionally) forwards them to a callback so the test can react."""

    def __init__(self, on_write=None):
        self.writes: list[bytes] = []
        self._on_write = on_write
        self._closing = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)
        if self._on_write is not None:
            self._on_write(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._closing = True

    def is_closing(self) -> bool:
        return self._closing


class _FakeStreamReader:
    """Stand-in for asyncio.StreamReader. Frames are pushed onto an async queue;
    a `None` marks EOF. Supports both ``async for line in reader`` and
    ``await reader.readline()`` (which is all run_hermes uses)."""

    def __init__(self):
        self._q: asyncio.Queue = asyncio.Queue()
        self._eof = False

    def feed_line(self, line: bytes) -> None:
        self._q.put_nowait(line if line.endswith(b"\n") else line + b"\n")

    def feed_eof(self) -> None:
        self._q.put_nowait(None)

    async def readline(self) -> bytes:
        if self._eof:
            return b""
        item = await self._q.get()
        if item is None:
            self._eof = True
            return b""
        return item

    def at_eof(self) -> bool:
        return self._eof and self._q.empty()

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        line = await self.readline()
        if not line:
            raise StopAsyncIteration
        return line


class _FakeProc:
    def __init__(self, stdin, stdout, stderr):
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.pid = 4242
        self.returncode = None
        self._done = asyncio.Event()

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode or 0

    def terminate(self) -> None:
        self.returncode = -15
        self._done.set()

    def kill(self) -> None:
        self.returncode = -9
        self._done.set()


def _frame(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")


def _su(update: dict, session_id="sess-abc") -> bytes:
    """A session/update notification frame."""
    return _frame({"jsonrpc": "2.0", "method": "session/update",
                   "params": {"sessionId": session_id, "update": update}})


def _make_fake_subprocess(frames: list[bytes], *, stdin_on_write=None, feed_eof=True):
    """Return (factory, proc) where factory is an async function suitable for
    monkeypatching ``asyncio.create_subprocess_exec``. ``frames`` are queued onto
    stdout in order; EOF is appended unless feed_eof=False."""
    stdout = _FakeStreamReader()
    for f in frames:
        stdout.feed_line(f)
    if feed_eof:
        stdout.feed_eof()
    stderr = _FakeStreamReader()
    stderr.feed_eof()
    stdin = _FakeStreamWriter(on_write=stdin_on_write)
    proc = _FakeProc(stdin, stdout, stderr)

    async def _factory(*args, **kwargs):
        return proc

    return _factory, proc


# Standard handshake responses. run_hermes with model=None allocates ids:
#   1 = initialize, 2 = session/new, then 3 = session/prompt.
_INIT_RESP = _frame({"jsonrpc": "2.0", "id": 1, "result": {
    "protocolVersion": 1, "agentInfo": {"name": "hermes-agent", "version": "0.13.0"},
    "agentCapabilities": {"loadSession": True}}})
_NEW_RESP = _frame({"jsonrpc": "2.0", "id": 2, "result": {
    "sessionId": "sess-abc",
    "models": {"availableModels": [], "currentModelId": "custom:qwen3.6:27b"}}})
_PROMPT_RESP = _frame({"jsonrpc": "2.0", "id": 3, "result": {
    "stopReason": "end_turn",
    "usage": {"inputTokens": 1234, "outputTokens": 56, "totalTokens": 1290}}})


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

async def test_hermes_conv_mode_persisted():
    """A conversation created with mode='hermes' round-trips through the DB
    (no schema migration required — the `mode` column has no CHECK)."""
    conv = await db.create_conversation("Hermes Chat", mode="hermes")
    await db.update_conversation_fields(conv["id"], local_model="qwen3.6:27b")
    fetched = await db.get_conversation(conv["id"])
    assert fetched["mode"] == "hermes"
    assert fetched["local_model"] == "qwen3.6:27b"


async def test_run_hermes_translates_a_turn(monkeypatch):
    """initialize -> session/new -> a thought chunk, a text chunk, a tool round-trip,
    a usage_update, then session/prompt returns. run_hermes should yield the
    expected Loom event types in order."""
    prompt_frames = [
        _su({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}}),
        _su({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hello"}}),
        _su({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": " world"}}),
        _su({"sessionUpdate": "tool_call", "toolCallId": "tc-1", "title": "terminal: echo hi",
             "kind": "execute", "content": [{"type": "content", "content": {"type": "text", "text": "$ echo hi"}}]}),
        _su({"sessionUpdate": "tool_call_update", "toolCallId": "tc-1", "kind": "execute",
             "status": "completed", "content": [{"type": "content", "content": {"type": "text", "text": "hi\n"}}]}),
        _su({"sessionUpdate": "usage_update", "size": 65536, "used": 2048}),
        _PROMPT_RESP,
    ]
    fake_proc_holder = {}

    def _on_stdin_write(data: bytes) -> None:
        req = json.loads(data.decode("utf-8"))
        proc = fake_proc_holder.get("proc")
        if proc is None:
            return
        if req.get("method") == "session/new":
            proc.stdout.feed_line(_NEW_RESP)
        elif req.get("method") == "session/prompt":
            for frame in prompt_frames:
                proc.stdout.feed_line(frame)
            proc.stdout.feed_eof()

    factory, proc = _make_fake_subprocess([_INIT_RESP], stdin_on_write=_on_stdin_write, feed_eof=False)
    fake_proc_holder["proc"] = proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", factory)

    got_proc, stream = await hermes_client.run_hermes(
        "hi", conv_id=1, model=None, cwd=".", loom_port=3001, hermes_exe="hermes")
    assert got_proc is proc
    events = [e async for e in stream]
    types = [e["type"] for e in events]

    assert "session_info" in types
    assert "thinking_delta" in types
    assert "text_delta" in types
    assert "tool_start" in types
    assert "tool_result" in types
    assert types[-1] == "result"
    # text deltas reconstruct the message
    text = "".join(e["text"] for e in events if e["type"] == "text_delta")
    assert text == "Hello world"
    # tool ids line up
    starts = [e for e in events if e["type"] == "tool_start"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert starts and results and starts[0]["tool_id"] == results[0]["tool_id"] == "tc-1"
    # final result carries the session id + stop reason + usage
    result_evt = events[-1]
    assert result_evt["session_id"] == "sess-abc"
    assert result_evt["stop_reason"] == "end_turn"
    usage_evts = [e for e in events if e["type"] == "usage"]
    assert usage_evts and usage_evts[-1]["output_tokens"] == 56


async def test_run_hermes_reuses_persistent_runtime(monkeypatch):
    """Two turns for the same Hermes home/exe should share one ACP process."""
    factory_calls = 0
    prompts_seen = 0
    fake_proc_holder = {}

    def _resp(req: dict, result: dict) -> bytes:
        return _frame({"jsonrpc": "2.0", "id": req["id"], "result": result})

    def _on_stdin_write(data: bytes) -> None:
        nonlocal prompts_seen
        req = json.loads(data.decode("utf-8"))
        proc = fake_proc_holder.get("proc")
        if proc is None:
            return
        method = req.get("method")
        if method == "session/new":
            proc.stdout.feed_line(_resp(req, {
                "sessionId": f"sess-{req['id']}",
                "models": {"availableModels": [], "currentModelId": "custom:qwen3.6:27b"},
            }))
        elif method == "session/prompt":
            prompts_seen += 1
            proc.stdout.feed_line(_su({
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": f"turn-{prompts_seen}"},
            }, session_id=f"sess-{req['id']}"))
            proc.stdout.feed_line(_resp(req, {
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            }))

    async def _factory(*args, **kwargs):
        nonlocal factory_calls
        factory_calls += 1
        stdout = _FakeStreamReader()
        stderr = _FakeStreamReader()
        stderr.feed_eof()
        stdin = _FakeStreamWriter(on_write=_on_stdin_write)
        proc = _FakeProc(stdin, stdout, stderr)
        fake_proc_holder["proc"] = proc
        stdout.feed_line(_INIT_RESP)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _factory)

    proc1, stream1 = await hermes_client.run_hermes(
        "one", conv_id=1, model=None, cwd=".", loom_port=3001, hermes_exe="hermes")
    events1 = [e async for e in stream1]
    proc2, stream2 = await hermes_client.run_hermes(
        "two", conv_id=1, model=None, cwd=".", loom_port=3001, hermes_exe="hermes")
    events2 = [e async for e in stream2]

    assert proc1 is proc2
    assert factory_calls == 1
    assert [e["text"] for e in events1 if e["type"] == "text_delta"] == ["turn-1"]
    assert [e["text"] for e in events2 if e["type"] == "text_delta"] == ["turn-2"]


async def test_permission_bridge_does_not_block_stream(monkeypatch):
    """THE v1 deadlock guard.

    A `session/request_permission` round-trip (browser approval, mocked here to
    take 0.4s) must NOT stall the stdout read loop. We feed
        before-delta, request_permission, after-delta
    all up front, with the permission POST artificially slow, and assert the
    'after' delta is yielded ~immediately after 'permission_request' (i.e. the
    read loop kept going) — not 0.4s later (which is what an inline blocking POST
    would cost). The realistic part: the agent only sends the prompt result
    *after* it receives the permission reply, so we gate `_PROMPT_RESP` on the
    bridge's reply frame appearing on stdin.
    """
    class _FakeResp:
        def json(self):
            return {"allow": True}

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):  # accepts verify=False etc.
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            await asyncio.sleep(0.4)
            return _FakeResp()

    monkeypatch.setattr(hermes_client.httpx, "AsyncClient", _FakeAsyncClient)

    perm_req = _frame({"jsonrpc": "2.0", "id": 99, "method": "session/request_permission",
                       "params": {"sessionId": "sess-abc",
                                  "toolCall": {"toolCallId": "perm", "title": "rm -rf /tmp/x", "kind": "execute"},
                                  "options": [
                                      {"optionId": "allow_once", "kind": "allow_once", "name": "Allow once"},
                                      {"optionId": "deny", "kind": "reject_once", "name": "Deny"}]}})
    stdin_writes: list[bytes] = []

    # When the bridge's reply for id 99 lands on stdin, let the "agent" finish.
    fake_proc_holder = {}

    def _on_stdin_write(data: bytes) -> None:
        stdin_writes.append(data)
        proc = fake_proc_holder.get("proc")
        if proc is None:
            return
        req = json.loads(data.decode("utf-8"))
        if req.get("method") == "session/new":
            proc.stdout.feed_line(_NEW_RESP)
        elif req.get("method") == "session/prompt":
            proc.stdout.feed_line(_su({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "before"}}))
            proc.stdout.feed_line(perm_req)
            proc.stdout.feed_line(_su({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "after"}}))
        elif req.get("id") == 99:
            proc.stdout.feed_line(_PROMPT_RESP)
            proc.stdout.feed_eof()

    factory, proc = _make_fake_subprocess([_INIT_RESP], stdin_on_write=_on_stdin_write, feed_eof=False)
    fake_proc_holder["proc"] = proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", factory)

    import time as _t
    _, stream = await hermes_client.run_hermes(
        "do it", conv_id=7, model=None, cwd=".", loom_port=3001, hermes_exe="hermes")

    events = []
    t_perm = t_after = None
    async for e in stream:
        events.append(e)
        if e["type"] == "permission_request":
            t_perm = _t.monotonic()
        elif e["type"] == "text_delta" and e["text"] == "after":
            t_after = _t.monotonic()

    types = [x["type"] for x in events]
    assert "permission_request" in types, types
    assert "after" in [e.get("text") for e in events if e["type"] == "text_delta"]
    # The read loop kept going while the slow POST was in flight: 'after' arrived
    # right after 'permission_request', not ~0.4s later.
    assert t_perm is not None and t_after is not None
    assert (t_after - t_perm) < 0.15, f"'after' lagged 'permission_request' by {t_after - t_perm:.3f}s — read loop blocked?"
    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["before", "after"]
    assert types[-1] == "result"

    # The bridge eventually replied to JSON-RPC id 99 with allow_once.
    replied = [w for w in stdin_writes if b'"id": 99' in w or b'"id":99' in w]
    assert replied, "permission bridge never sent its reply frame"
    reply = json.loads(replied[-1].decode("utf-8"))
    assert reply["id"] == 99
    assert reply["result"]["outcome"]["outcome"] == "selected"
    assert reply["result"]["outcome"]["optionId"] == "allow_once"


# --------------------------------------------------------------------------- #
# is_first_turn / context-duplication regression (RC1)
# --------------------------------------------------------------------------- #
# On a resume turn, session/fork already populated Hermes' server-side session
# with the conversation history. Re-injecting <loom_branch_info> (which carries
# a 100-char preview of EVERY branch message) would feed the model every prior
# message twice. Callers pass is_first_turn=(not use_resume); this locks that
# _prepare_hermes_prompt honors the flag and returns the bare prompt when False.

async def test_prepare_hermes_prompt_first_turn_injects_branch_info():
    """is_first_turn=True wraps the prompt with contract + branch_info."""
    branch = [
        {"id": 1, "parent_id": None, "role": "user", "content": "hello"},
        {"id": 2, "parent_id": 1, "role": "assistant", "content": "hi there"},
    ]
    out = hermes_client._prepare_hermes_prompt(
        "do the thing", branch=branch, model="custom:diffusiongemma-26b",
        is_first_turn=True,
    )
    assert "<loom_branch_info>" in out
    assert "<user_task>" in out
    assert "do the thing" in out


async def test_prepare_hermes_prompt_resume_turn_is_bare():
    """is_first_turn=False returns the bare prompt — no branch_info, no contract.

    This is the RC1 fix: a forked session already holds the history server-side,
    so the model must NOT see <loom_branch_info> again.
    """
    branch = [
        {"id": 1, "parent_id": None, "role": "user", "content": "hello"},
        {"id": 2, "parent_id": 1, "role": "assistant", "content": "hi there"},
    ]
    out = hermes_client._prepare_hermes_prompt(
        "do the next thing", branch=branch, model="custom:diffusiongemma-26b",
        is_first_turn=False,
    )
    # Bare prompt only — no duplication of branch history into the model context.
    assert out == "do the next thing"
    assert "<loom_branch_info>" not in out
    assert "<loom_agent_contract>" not in out
    assert "<dream_tooling_rules>" not in out
