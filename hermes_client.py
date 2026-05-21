"""Hermes Agent ACP subprocess wrapper — drives `hermes acp` over stdio JSON-RPC.

Native-Windows path: Hermes 0.13.0 is installed under %LOCALAPPDATA%\\hermes
(via NousResearch's install.ps1 or a manual `uv pip install -e .[acp]`), so we
spawn `hermes.exe acp` directly with `asyncio.create_subprocess_exec` — no WSL
wrapper, no inner-PID dance, no path translation, no NAT-vs-mirrored networking.

`run_hermes()` returns `(proc, async_generator)` mirroring `claude_client.run_claude`:
the generator yields Loom event dicts of the same shape `claude_client._process_event`
produces, so `server.py`'s stream loop can consume Hermes turns the same way it
consumes Claude Code turns.

Process model: one `hermes acp` child per generation (per-turn). v1 forks via
history replay — every turn opens a fresh ACP session and the full branch is
rendered into the prompt string by the caller, so Loom's fork-every-turn
branching invariant holds trivially. (ACP session/load + session/fork is a
later optimisation.)

ACP wire format reference: tools/probe_output.txt, plus acp_adapter/{server,events,
tools,permissions}.py in the Hermes repo. Field names are camelCase on the wire.

Gotcha worth keeping: Hermes 0.13.0 on Windows can leak a human-readable line
to STDOUT instead of stderr (e.g. a sudo UAC-prompt timeout prints
"⏱ Timeout - continuing without sudo"). The read loop must skip non-JSON
stdout lines, never crash on them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

import httpx

log = logging.getLogger(__name__)

# How long Hermes waits on a request_permission before auto-denying is 60s on
# the agent side; give the browser modal a generous window before our HTTP POST
# gives up so a slow human still beats Hermes' own timeout only barely. (Hermes
# treating it as "deny" on its own timeout is the worst case — we don't make it
# worse.) Matches the v2 plan's 900s for parity with the CC permission hook.
_PERMISSION_HTTP_TIMEOUT = 900.0

# Cap on stdout line size we'll buffer (matches claude_client's subprocess limit).
_STREAM_LIMIT = 16 * 1024 * 1024


def _loom_mcp_servers() -> list[dict]:
    """ACP `mcpServers` to register on every Hermes session.

    Hermes' built-in ``web_search`` / ``web_extract`` need API keys (Exa,
    Firecrawl, Tavily, …) that aren't set. Loom ships a keyless MCP server —
    ``mcp_web_tools.py`` (DuckDuckGo search + trafilatura fetch, the same one
    Claude Code uses for local models) — so we hand it to Hermes here. The ACP
    stdio MCP shape is ``{name, command, args, env:[{name,value}]}``; Hermes
    wraps registration in a try/except, so a bad entry just logs a warning
    rather than failing ``session/new``.
    """
    web_tools = Path(__file__).resolve().parent / "mcp_web_tools.py"
    if not web_tools.is_file():
        return []
    return [{
        "name": "web-tools",
        "command": sys.executable,
        "args": [str(web_tools)],
        "env": [],
    }]


def default_hermes_exe(hermes_home: str | None = None) -> str:
    """Best-effort path to the `hermes` CLI: the venv Scripts binary under the
    install root, falling back to bare `hermes` on PATH."""
    home = hermes_home or os.environ.get(
        "HERMES_HOME",
        os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes"),
    )
    exe_name = "hermes.exe" if os.name == "nt" else "hermes"
    cand = os.path.join(home, "hermes-agent", ".venv", "Scripts" if os.name == "nt" else "bin", exe_name)
    return cand if os.path.exists(cand) else "hermes"


# --------------------------------------------------------------------------- #
# JSON-RPC helpers
# --------------------------------------------------------------------------- #

class _RpcConn:
    """Minimal JSON-RPC 2.0 framing over a subprocess's stdin/stdout pipes.

    Newline-delimited JSON, one message per line. Outbound: requests (with an
    auto-incremented id) and responses (to the agent's own requests). Inbound is
    drained by the caller's read loop; this class only owns id allocation and
    a future map for matching responses to our outbound requests.
    """

    def __init__(self, proc: asyncio.subprocess.Process):
        self._proc = proc
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}

    def _alloc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _write(self, msg: dict) -> None:
        if self._proc.stdin is None:
            raise RuntimeError("hermes acp stdin is closed")
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def request(self, method: str, params: dict | None = None,
                      timeout: float | None = None) -> Any:
        """Send a JSON-RPC request and await its result. The read loop must call
        `resolve_response(msg)` for matching ids."""
        rid = self._alloc_id()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)
        try:
            if timeout is not None:
                return await asyncio.wait_for(fut, timeout)
            return await fut
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)

    async def respond(self, req_id: Any, result: Any = None, error: dict | None = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result if result is not None else {}
        await self._write(msg)

    def resolve_response(self, msg: dict) -> bool:
        """If `msg` is a response to one of our requests, resolve its future and
        return True; otherwise return False (caller handles it as a request/notification)."""
        rid = msg.get("id")
        if rid is None or "method" in msg:
            return False
        fut = self._pending.get(rid)
        if fut is None or fut.done():
            return False
        if "error" in msg:
            fut.set_exception(RuntimeError(f"hermes acp error: {msg['error']}"))
        else:
            fut.set_result(msg.get("result"))
        return True


# --------------------------------------------------------------------------- #
# Content-block helpers (ACP -> plain text)
# --------------------------------------------------------------------------- #

def _block_text(block: Any) -> str:
    """Pull text out of an ACP content block ({type:"text",text:...}) or a
    ToolCallContent ({type:"content",content:{...}} / {type:"diff",...})."""
    if not isinstance(block, dict):
        return str(block)
    t = block.get("type")
    if t == "text":
        return block.get("text", "")
    if t == "content":
        return _block_text(block.get("content"))
    if t == "diff":
        # Render a diff block compactly.
        path = block.get("path", "")
        old = block.get("oldText") or block.get("old_text") or ""
        new = block.get("newText") or block.get("new_text") or ""
        head = f"--- {path}\n+++ {path}\n" if path else ""
        return head + (f"- {old}\n" if old else "") + (f"+ {new}" if new else "")
    if "content" in block:
        return _block_text(block["content"])
    return block.get("text", "")


def _content_to_text(content: Any) -> str:
    """Flatten a content value that may be a single block or a list of blocks."""
    if isinstance(content, list):
        return "".join(_block_text(b) for b in content)
    return _block_text(content)


# --------------------------------------------------------------------------- #
# Permission bridge
# --------------------------------------------------------------------------- #

def _pick_option(options: list, kind: str) -> Optional[str]:
    for opt in options or []:
        if isinstance(opt, dict) and (opt.get("kind") or "").lower() == kind:
            return opt.get("optionId") or opt.get("option_id") or opt.get("id")
    return None


async def _bridge_permission(rpc: _RpcConn, req_id: Any, params: dict,
                             conv_id: int, loom_port: int) -> None:
    """Run the browser approval round-trip in its own task, then reply to the
    agent's `session/request_permission` request on the same JSON-RPC id.

    Crucially, the main stdout read loop is never blocked — multiple in-flight
    permission requests are independent tasks. (This is the v1 deadlock guard.)
    """
    tool_call = params.get("toolCall") or params.get("tool_call") or {}
    tool_name = tool_call.get("title") or tool_call.get("name") or "HermesTool"
    options = params.get("options") or []

    body = {
        "loom_conv_id": conv_id,
        "tool_name": tool_name,
        "tool_input": {"toolCall": tool_call},
        "hook_event_name": "PreToolUse",
    }
    # Detect protocol (HTTPS if certs exist). Resolve relative to this script.
    _certs_dir = Path(__file__).parent / "certs"
    protocol = "https" if (_certs_dir / "cert.pem").exists() and (_certs_dir / "key.pem").exists() else "http"

    allow = False
    try:
        async with httpx.AsyncClient(verify=False) as c:
            r = await c.post(
                f"{protocol}://127.0.0.1:{loom_port}/api/cc-permission",
                json=body, timeout=_PERMISSION_HTTP_TIMEOUT,
            )
            resp = r.json()
        allow = bool(resp.get("allow"))
    except Exception as e:  # noqa: BLE001 — any failure -> deny (Hermes does the same)
        log.warning("hermes permission bridge failed (conv %s): %s", conv_id, e)
        allow = False

    if allow:
        # v1: allow_always is mapped to allow_once (no persistent grants).
        opt_id = _pick_option(options, "allow_once") or _pick_option(options, "allow_always")
        outcome = ({"outcome": "selected", "optionId": opt_id} if opt_id
                   else {"outcome": "cancelled"})
    else:
        opt_id = _pick_option(options, "reject_once") or _pick_option(options, "reject_always")
        outcome = ({"outcome": "selected", "optionId": opt_id} if opt_id
                   else {"outcome": "cancelled"})

    try:
        await rpc.respond(req_id, result={"outcome": outcome})
    except Exception as e:  # noqa: BLE001
        log.warning("hermes permission reply failed (conv %s): %s", conv_id, e)


# --------------------------------------------------------------------------- #
# session/update dispatch
# --------------------------------------------------------------------------- #

def _dispatch_session_update(update: dict, state: dict) -> list[dict]:
    """Map one ACP `session/update` payload to zero or more Loom event dicts.

    `state` carries cross-call context: a set of tool_call ids we've already
    emitted a `tool_start` for (so the same id arriving on `tool_call_update`
    becomes a `tool_result`, not a duplicate start).
    """
    kind = update.get("sessionUpdate") or update.get("session_update")
    events: list[dict] = []

    if kind == "agent_message_chunk":
        txt = _content_to_text(update.get("content"))
        if txt:
            events.append({"type": "text_delta", "text": txt})
    elif kind == "agent_thought_chunk":
        txt = _content_to_text(update.get("content"))
        if txt:
            events.append({"type": "thinking_delta", "text": txt})
    elif kind == "user_message_chunk":
        # Only seen during history replay / queued-prompt echo — ignore; Loom
        # owns the user side of the transcript.
        pass
    elif kind == "tool_call":
        tc_id = update.get("toolCallId") or update.get("tool_call_id") or ""
        title = update.get("title") or update.get("kind") or "tool"
        seen = state.setdefault("tool_calls", set())
        seen.add(tc_id)
        events.append({"type": "tool_start", "name": title, "tool_id": tc_id})
        body = _content_to_text(update.get("content"))
        if body:
            events.append({"type": "tool_input_delta", "json": body, "tool_id": tc_id})
    elif kind == "tool_call_update":
        tc_id = update.get("toolCallId") or update.get("tool_call_id") or ""
        seen = state.setdefault("tool_calls", set())
        if tc_id not in seen:
            # A completion without a start we saw — synthesize a start so the UI
            # has a block to attach the result to.
            seen.add(tc_id)
            events.append({"type": "tool_start",
                           "name": update.get("title") or update.get("kind") or "tool",
                           "tool_id": tc_id})
        status = update.get("status")
        body = _content_to_text(update.get("content"))
        if status in (None, "completed", "failed", "error") or body:
            events.append({"type": "tool_result", "content": body, "tool_id": tc_id,
                           "is_error": status in ("failed", "error")})
    elif kind == "usage_update":
        # Native context-pressure indicator (used / size). Not per-call token
        # counts — surface as a lightweight status so the UI's context meter can
        # update without us pretending it's an Anthropic-style usage event.
        used = update.get("used")
        size = update.get("size")
        if used is not None:
            events.append({"type": "hermes_usage_update", "used": used, "size": size})
    elif kind == "available_commands_update":
        events.append({"type": "hermes_commands",
                       "commands": update.get("availableCommands")
                       or update.get("available_commands") or []})
    elif kind == "plan":
        events.append({"type": "plan_update", "content": _content_to_text(update.get("content"))})
    else:
        events.append({"type": "hermes_raw_update", "kind": kind, "data": update})

    return events


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

async def run_hermes(
    prompt: str | list,
    *,
    conv_id: int = 0,
    model: str | None = None,
    cwd: str = ".",
    loom_port: int = 3000,
    hermes_exe: str | None = None,
    hermes_home: str | None = None,
    system: str | None = None,
):
    """Spawn `hermes acp`, run one prompt turn, and return ``(proc, event_stream)``.

    Args:
        prompt: the fully-rendered user turn (caller bakes in history + persona).
        conv_id: Loom conversation id (for the permission bridge POST body).
        model: optional Ollama model name; if given, sent via ACP `session/set_model`
               as ``custom:<model>``. None -> Hermes uses its config.yaml default.
        cwd: working directory the agent operates in. Translated to a forward-slash
             path on the wire (Hermes runs its terminal tool via Git Bash).
        loom_port: port the Loom server listens on (for ``/api/cc-permission``).
        hermes_exe / hermes_home: override the resolved CLI path / install root.

    Yields Loom event dicts of the same shape ``claude_client._process_event``
    produces:
        {type:"session_info", session_id, model}
        {type:"text_delta", text}
        {type:"thinking_delta", text}
        {type:"tool_start", name, tool_id}
        {type:"tool_input_delta", json, tool_id}
        {type:"tool_result", content, tool_id, is_error}
        {type:"usage", input_tokens, output_tokens}
        {type:"hermes_usage_update", used, size}
        {type:"hermes_commands", commands}
        {type:"plan_update", content}
        {type:"error", error}
        {type:"result", session_id, stop_reason, duration_ms, num_turns}
    """
    exe = hermes_exe or default_hermes_exe(hermes_home)
    home = hermes_home or os.environ.get(
        "HERMES_HOME",
        os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes"),
    )

    if exe.endswith((".py",)) or exe in ("python", "python.exe", sys.executable):
        cmd = [exe, "-m", "acp_adapter"]
    else:
        cmd = [exe, "acp"]

    env = {**os.environ, "HERMES_HOME": home, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}

    # cwd on disk for the child process; forward-slash form for the ACP payload.
    work_dir = cwd if (cwd and os.path.isdir(cwd)) else os.getcwd()
    acp_cwd = str(Path(work_dir)).replace("\\", "/")

    log.info("[Hermes] spawning: %s (cwd=%s, model=%s, HERMES_HOME=%s)", cmd, work_dir, model, home)
    kwargs = {}
    if sys.platform == "win32":
        import subprocess
        # Use CREATE_NO_WINDOW (0x08000000) and CREATE_NEW_PROCESS_GROUP (0x00000200)
        kwargs["creationflags"] = 0x08000000 | 0x00000200

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=work_dir,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_STREAM_LIMIT,
        **kwargs
    )

    async def _drain_stderr() -> None:
        assert proc.stderr is not None
        async for line in proc.stderr:
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                print(f"[Hermes-stderr] {text}")
    asyncio.create_task(_drain_stderr())

    rpc = _RpcConn(proc)
    state: dict = {}
    pending_tasks: set[asyncio.Task] = set()

    def _spawn_bridge(req_id: Any, params: dict) -> None:
        task = asyncio.create_task(_bridge_permission(rpc, req_id, params, conv_id, loom_port))
        pending_tasks.add(task)
        task.add_done_callback(pending_tasks.discard)

    async def _event_stream() -> AsyncGenerator[dict, None]:
        import time as _time
        t0 = _time.time()
        session_id = ""
        prompt_req_id: Optional[int] = None
        finished = False
        try:
            # --- handshake ---
            init_result = await rpc_request_via_reader(
                rpc, "initialize", {
                    "protocolVersion": 1,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                           "terminal": False},
                    "clientInfo": {"name": "loom", "version": "1"},
                }, proc, state)
            agent_info = (init_result or {}).get("agentInfo") or {}
            log.info("[Hermes] connected: %s", agent_info)

            new_result = await rpc_request_via_reader(
                rpc, "session/new", {"cwd": acp_cwd, "mcpServers": _loom_mcp_servers()}, proc, state)
            session_id = (new_result or {}).get("sessionId") or (new_result or {}).get("session_id") or ""
            yield {"type": "session_info", "session_id": session_id,
                   "model": ((new_result or {}).get("models") or {}).get("currentModelId", "")}

            if model:
                try:
                    # If it has a colon, it's likely an Ollama model (e.g. qwen:27b).
                    # Hermes' 'custom' provider often mangles these by splitting on ':'.
                    # Try 'ollama:' prefix which Hermes supports more natively for these.
                    prefix = "ollama" if ":" in model else "custom"
                    await rpc_request_via_reader(
                        rpc, "session/set_model",
                        {"sessionId": session_id, "modelId": f"{prefix}:{model}"}, proc, state)
                except Exception as e:  # noqa: BLE001
                    log.warning("[Hermes] set_model(%s) failed: %s", model, e)

            # --- prompt turn ---
            # Issue the prompt request, then drain until its response arrives.
            prompt_req_id = rpc._alloc_id()
            prompt_fut: asyncio.Future = asyncio.get_event_loop().create_future()
            rpc._pending[prompt_req_id] = prompt_fut
            prompt_payload = prompt if isinstance(prompt, list) else [{"type": "text", "text": prompt}]
            await rpc._write({"jsonrpc": "2.0", "id": prompt_req_id, "method": "session/prompt",
                              "params": {"sessionId": session_id,
                                         "prompt": prompt_payload}})

            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.rstrip(b"\r\n")
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # Hermes can leak non-JSON to stdout (e.g. sudo timeout banner).
                    log.warning("[Hermes] non-JSON stdout: %r", line[:200])
                    continue

                # Response to one of our requests?
                if rpc.resolve_response(msg):
                    if prompt_fut.done():
                        # session/prompt completed -> turn done.
                        try:
                            result = prompt_fut.result()
                        except Exception as e:  # noqa: BLE001
                            yield {"type": "error", "error": f"hermes prompt failed: {e}"}
                            result = {}
                        usage = (result or {}).get("usage") or {}
                        if usage:
                            yield {"type": "usage",
                                   "input_tokens": usage.get("inputTokens", usage.get("input_tokens", 0)),
                                   "output_tokens": usage.get("outputTokens", usage.get("output_tokens", 0))}
                        yield {"type": "result",
                               "session_id": session_id,
                               "stop_reason": (result or {}).get("stopReason") or (result or {}).get("stop_reason") or "end_turn",
                               "duration_ms": int((_time.time() - t0) * 1000),
                               "num_turns": 1}
                        finished = True
                        break
                    continue

                method = msg.get("method")
                mid = msg.get("id")

                # Incoming request from the agent.
                if method and mid is not None:
                    if method in ("session/request_permission", "session/requestPermission"):
                        params = msg.get("params") or {}
                        tc = params.get("toolCall") or params.get("tool_call") or {}
                        yield {"type": "permission_request",
                               "request_id": mid,
                               "tool_name": tc.get("title") or tc.get("name") or "HermesTool",
                               "tool_input": tc}
                        _spawn_bridge(mid, params)
                    elif method and (method.startswith("fs/") or method.startswith("terminal/")):
                        await rpc.respond(mid, error={"code": -32601, "message": "not supported by loom client"})
                    else:
                        await rpc.respond(mid, result={})
                    continue

                # Notification from the agent.
                if method == "session/update":
                    update = (msg.get("params") or {}).get("update") or {}
                    for evt in _dispatch_session_update(update, state):
                        yield evt
                    continue
                # Other notifications: ignore quietly.

            if not finished:
                # stdout closed before the prompt response — surface what we can.
                if proc.stdout is not None and proc.stdout.at_eof():
                    yield {"type": "error", "error": "hermes acp exited before completing the turn"}
                yield {"type": "result", "session_id": session_id,
                       "stop_reason": "incomplete",
                       "duration_ms": int((_time.time() - t0) * 1000), "num_turns": 1}
        finally:
            for task in list(pending_tasks):
                task.cancel()
            try:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:
                pass

    return proc, _event_stream()


async def rpc_request_via_reader(rpc: _RpcConn, method: str, params: dict,
                                 proc: asyncio.subprocess.Process, state: dict,
                                 timeout: float = 60.0) -> Any:
    """Send a JSON-RPC request and pump stdout until its response arrives.

    Used for the pre-turn handshake calls (initialize / session/new / session/set_model)
    where no event yielding happens yet. Any `session/update` notifications that
    arrive in the meantime are dispatched into `state` but their events are
    dropped (handshake produces no user-visible stream). Incoming agent requests
    during the handshake are answered with empty results.
    """
    rid = rpc._alloc_id()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rpc._pending[rid] = fut
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    await rpc._write(msg)

    assert proc.stdout is not None
    deadline = asyncio.get_event_loop().time() + timeout
    while not fut.done():
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            rpc._pending.pop(rid, None)
            raise asyncio.TimeoutError(f"hermes {method} timed out")
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            rpc._pending.pop(rid, None)
            raise asyncio.TimeoutError(f"hermes {method} timed out")
        if not raw:
            rpc._pending.pop(rid, None)
            raise RuntimeError(f"hermes acp closed stdout during {method}")
        line = raw.rstrip(b"\r\n")
        if not line:
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            log.warning("[Hermes] non-JSON stdout during handshake: %r", line[:200])
            continue
        if rpc.resolve_response(m):
            continue
        meth = m.get("method")
        mid = m.get("id")
        if meth and mid is not None:
            await rpc.respond(mid, result={})
        elif meth == "session/update":
            _dispatch_session_update((m.get("params") or {}).get("update") or {}, state)
    rpc._pending.pop(rid, None)
    return fut.result()


async def cancel_hermes(proc: asyncio.subprocess.Process) -> None:
    """Terminate a running `hermes acp` subprocess and its tree.

    Native Windows: terminate() only kills the top process, so use taskkill /T
    to take the Git-Bash terminal-tool children with it. (No WSL inner-PID
    bookkeeping needed — that whole class of problem went away with the native
    install.)"""
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        import subprocess
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=5)
        except Exception:
            proc.kill()
    else:
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
