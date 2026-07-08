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
import re
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


# Hermes provider slugs that `parse_model_input` (hermes_cli/models.py) treats
# as `<provider>:<model>` heads. Anything else with a colon is interpreted as
# part of the model name, not a provider switch.
_HERMES_PROVIDER_PREFIXES = frozenset({
    "custom", "openrouter", "nous", "anthropic", "openai", "google",
    "mistral", "xai", "zai", "groq", "cerebras", "bedrock",
})


def _collect_current_turn_image_blocks(branch: list[dict] | None) -> list[dict]:
    """Build ACP ``ImageContentBlock``s for the latest user message's attachments.

    Older user messages are not included — the rendered prompt string in
    ``_prepare_hermes_prompt`` summarises history textually, and once
    ``session/load`` lands Hermes will carry the past server-side anyway. We
    only need the actual bytes for the image the user is asking about *now*.

    Borrows the data-URL encoder from ``llama_client`` (with its WebP→JPEG
    conversion path) so Hermes's vision pipeline sees the same input format as
    direct llama-server calls.
    """
    if not branch:
        return []

    latest_with_image: dict | None = None
    for msg in reversed(branch):
        if msg.get("role") != "user":
            continue
        if msg.get("image_path"):
            latest_with_image = msg
            break

    if latest_with_image is None:
        return []

    try:
        import llama_client
        paths = llama_client._parse_image_paths(latest_with_image.get("image_path"))
    except Exception as e:  # noqa: BLE001
        log.debug("[Hermes] _parse_image_paths failed: %s", e)
        return []

    blocks: list[dict] = []
    for path in paths:
        try:
            data_url = llama_client._image_to_data_url(path)
        except Exception as e:  # noqa: BLE001
            log.warning("[Hermes] image conversion failed for %s: %s", path, e)
            continue
        if not data_url:
            continue
        mime = "image/jpeg"
        if data_url.startswith("data:"):
            head = data_url.split(";", 1)[0]
            if ":" in head:
                mime = head.split(":", 1)[1] or mime
        blocks.append({"type": "image", "data": data_url, "mimeType": mime})

    return blocks


def _loom_model_to_hermes(model: str | None) -> str | None:
    """Map Loom's local-model selection to a Hermes ACP ``modelId``.

    Loom passes whatever its chat UI's local-model dropdown selects — usually a
    ``.gguf`` filename or a server-registered ID like ``qwen3.6:27b``. Hermes
    ACP wants ``<provider>:<model>`` where the provider is ``custom`` for our
    llama-server backend.

      - ``None`` → ``None`` (Hermes falls back to its ``config.yaml`` default).
      - Already provider-qualified (``custom:…``, ``openrouter:…`` etc.) → as-is.
      - ``.gguf`` filename → resolved via ``llama_client._resolve_model`` to a
        server-registered ID, then prefixed with ``custom:``.
      - Anything else → wrapped as ``custom:<model>``.

    Hermes 0.13.0's parser only treats the FIRST colon as a provider delimiter,
    and only when the head matches a known provider, so ``custom:qwen3.6:27b``
    round-trips correctly without the old ``custom:`` prefix workaround.
    """
    if not model:
        return None
    if ":" in model:
        head = model.split(":", 1)[0].lower()
        if head in _HERMES_PROVIDER_PREFIXES:
            return model
    try:
        import llama_client
        resolved = llama_client._resolve_model(model)
    except Exception as e:  # noqa: BLE001
        log.debug("[Hermes] _resolve_model(%s) failed: %s", model, e)
        resolved = model
    # Ensure the resolved ID is what llama-server actually registered.
    # llama-server registers loaded models by their GGUF internal filename.
    # _resolve_model() handles the mapping chain: runtime map → config → filename.
    return f"custom:{resolved}"


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
        self._backlog: dict[int, dict] = {}

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
        backlogged = self._backlog.pop(rid, None)
        if backlogged is not None:
            self.resolve_response(backlogged)
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
        if fut is None:
            self._backlog[rid] = msg
            return True
        if fut.done():
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


_DREAM_THOUGHT_BLOCK_RE = re.compile(
    r"<\|channel>thought\s*(.*?)<channel\|>",
    re.IGNORECASE | re.DOTALL,
)


def _dream_channel_events(text: str, state: dict | None = None) -> list[dict]:
    """Convert leaked DiffusionGemma thought channels into Loom events."""
    if state is not None and state.get("dream_channel_buffer"):
        text = state.pop("dream_channel_buffer") + text

    if "<|channel>" not in text and "<channel|>" not in text:
        return [{"type": "text_delta", "text": text}] if text else []

    normalized = text.replace("\r\n", "\n")
    events: list[dict] = []
    partial = re.search(r"<\|channel>thought", normalized, re.IGNORECASE)
    if partial and "<channel|>" not in normalized[partial.start():]:
        before = normalized[:partial.start()]
        if before.strip():
            events.append({"type": "text_delta", "text": before})
        if state is not None:
            state["dream_channel_buffer"] = normalized[partial.start():]
            return events

    cursor = 0
    matched = False
    for match in _DREAM_THOUGHT_BLOCK_RE.finditer(normalized):
        matched = True
        before = normalized[cursor:match.start()]
        if before.strip():
            events.append({"type": "text_delta", "text": before})
        thought = match.group(1).strip()
        if thought:
            events.append({"type": "thinking_delta", "text": thought})
        cursor = match.end()

    if matched:
        after = normalized[cursor:]
        if after.strip():
            events.append({"type": "text_delta", "text": after})
        return events

    if normalized.strip() in {"<|channel>", "<channel|>", "thought"}:
        return []
    return [{"type": "text_delta", "text": normalized}]


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
        events.extend(_dream_channel_events(_content_to_text(update.get("content")), state))
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


def _prepare_hermes_prompt(
    prompt: str,
    branch: list[dict] | None = None,
    model: str | None = None,
    *,
    is_first_turn: bool = True,
) -> str:
    """Inject the Loom contract and positionality for Hermes.

    When ``is_first_turn`` is True (the default), wraps the prompt with the
    Loom agent contract and a ``<loom_branch_info>`` positionality block —
    Hermes is in a fresh session and needs orientation. When False, returns
    the bare ``prompt`` because Hermes already has the session's rolling
    context server-side and only needs the new user turn.

    Callers pass ``is_first_turn=(not use_resume)``: a resume turn forks a
    session that already holds the conversation history, so re-injecting
    ``<loom_branch_info>`` would feed the model every prior message twice.
    Fresh sessions (no ``resume_session_id``, or the resume-failed fallback
    that opens ``session/new``) get the full orientation block.
    """
    if not is_first_turn:
        return prompt

    from loom_agent_prompt import load_loom_agent_prompt
    contract = load_loom_agent_prompt()
    if not contract and not branch and not model:
        return prompt

    contract_header = ""
    if contract:
        contract_header = (
            f"<loom_agent_contract provider=\"hermes\">\n"
            f"{contract}\n"
            f"</loom_agent_contract>\n\n"
        )

    # NOTE: no tool-call format nudge is injected here. Each backend teaches
    # tool format itself: the dream (DiffusionGemma) path is text-only and the
    # nuspy adapter's own system prompt (agent/tools/registry.py render_for_prompt)
    # documents the <tool name="..."> XML format + parse_tool_calls parses it;
    # the llama/Prometheus path uses Hermes ACP's native tool-calling. A prior
    # attempt to add a <hermes_tooling_rules> nudge here contaminated the dream
    # prompt (model emitted bare tool calls with no prose, tools didn't execute)
    # and is reverted. Do NOT re-add a nudge without verifying against a live
    # dream turn's agent.log — see project_hermes_toolcall_hallucination_nudge.

    branch_info = ""
    if branch or model:
        conv_id = branch[0].get("conversation_id") if branch else "unknown"
        leaf_id = branch[-1].get("id") if branch else "unknown"
        path_str = " -> ".join(f"msg_{m.get('id')}" for m in branch) if branch else "None"

        steps = []
        if branch:
            for msg in branch:
                msg_id = msg.get("id")
                parent_id = msg.get("parent_id")
                role = msg.get("role")

                content_preview = msg.get("summary") or msg.get("content", "")
                if isinstance(content_preview, str):
                    content_preview = content_preview.strip().replace("\n", " ")
                    if len(content_preview) > 100:
                        content_preview = content_preview[:97] + "..."
                else:
                    content_preview = ""

                parent_str = f"parent: msg_{parent_id}" if parent_id is not None else "parent: None"
                steps.append(f"  * [msg_{msg_id}] (role: {role}, {parent_str}): {content_preview}")

        steps_str = "\n".join(steps) if steps else "  (No messages in branch yet)"

        branch_info = (
            f"<loom_branch_info>\n"
            f"Note: Hermes is built different and operates with decentralized branches. "
            f"You are operating on a specific branch of the conversation tree. "
            f"Compare this branch path and model to tell if you have stepped into a different/forked branch or changed models:\n"
            f"Conversation ID: {conv_id}\n"
            f"Active Node ID: {leaf_id}\n"
            f"Active Model: {model or 'default'}\n"
            f"Active Branch Path: {path_str}\n"
            f"Branch Messages:\n"
            f"{steps_str}\n"
            f"</loom_branch_info>\n\n"
        )

    return (
        f"{contract_header}"
        f"{branch_info}"
        f"<user_task>\n"
        f"{prompt}\n"
        f"</user_task>"
    )


# --------------------------------------------------------------------------- #
# Persistent ACP runtime
# --------------------------------------------------------------------------- #

class _HermesTurn:
    def __init__(self, conv_id: int, loom_port: int):
        self.conv_id = conv_id
        self.loom_port = loom_port
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self.state: dict = {}
        self.permission_tasks: set[asyncio.Task] = set()


class _HermesAcpRuntime:
    """Long-lived `hermes acp` process for one Hermes home/executable.

    ACP stdout has exactly one reader. Public turns are serialized with a lock
    and receive streamed `session/update` events through the active turn queue.
    """

    def __init__(self, *, exe: str, home: str, cwd: str):
        self.exe = exe
        self.home = home
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self.rpc: _RpcConn | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._active_turn: _HermesTurn | None = None
        self._agent_info: dict = {}

    def _cmd(self) -> list[str]:
        if self.exe.endswith((".py",)) or self.exe in ("python", "python.exe", sys.executable):
            return [self.exe, "-m", "acp_adapter"]
        return [self.exe, "acp"]

    def _is_alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self._is_alive() and self.rpc is not None:
                return

            await self.stop(remove=False)

            env = {**os.environ, "HERMES_HOME": self.home, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
            work_dir = self.cwd if (self.cwd and os.path.isdir(self.cwd)) else os.getcwd()
            kwargs = {}
            if sys.platform == "win32":
                # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
                kwargs["creationflags"] = 0x08000000 | 0x00000200

            cmd = self._cmd()
            log.info("[Hermes] starting persistent ACP: %s (cwd=%s, HERMES_HOME=%s)", cmd, work_dir, self.home)
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=work_dir,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STREAM_LIMIT,
                **kwargs,
            )
            self.rpc = _RpcConn(self.proc)
            _PROC_RUNTIMES[self.proc.pid] = self
            self._reader_task = asyncio.create_task(self._reader_loop())
            self._stderr_task = asyncio.create_task(self._drain_stderr())

            init_result = await self.rpc.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "loom", "version": "1"},
                },
                timeout=60.0,
            )
            self._agent_info = (init_result or {}).get("agentInfo") or {}
            log.info("[Hermes] persistent ACP connected: %s", self._agent_info)

    async def _drain_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        async for line in proc.stderr:
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                print(f"[Hermes-stderr] {text}")

    async def _reader_loop(self) -> None:
        proc = self.proc
        rpc = self.rpc
        if proc is None or proc.stdout is None or rpc is None:
            return
        try:
            async for raw in proc.stdout:
                line = raw.rstrip(b"\r\n")
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("[Hermes] non-JSON stdout: %r", line[:200])
                    continue

                if rpc.resolve_response(msg):
                    continue

                method = msg.get("method")
                mid = msg.get("id")
                if method and mid is not None:
                    await self._handle_agent_request(mid, method, msg.get("params") or {})
                    continue

                if method == "session/update":
                    turn = self._active_turn
                    if turn is None:
                        continue
                    update = (msg.get("params") or {}).get("update") or {}
                    for evt in _dispatch_session_update(update, turn.state):
                        await turn.queue.put(evt)
        finally:
            if rpc is not None:
                for fut in list(rpc._pending.values()):
                    if not fut.done():
                        fut.set_exception(RuntimeError("hermes acp stdout closed"))
            turn = self._active_turn
            if turn is not None:
                await turn.queue.put({"type": "error", "error": "hermes acp exited"})

    async def _handle_agent_request(self, req_id: Any, method: str, params: dict) -> None:
        assert self.rpc is not None
        if method in ("session/request_permission", "session/requestPermission"):
            turn = self._active_turn
            if turn is None:
                await self.rpc.respond(req_id, result={"outcome": {"outcome": "cancelled"}})
                return
            tc = params.get("toolCall") or params.get("tool_call") or {}
            await turn.queue.put({
                "type": "permission_request",
                "request_id": req_id,
                "tool_name": tc.get("title") or tc.get("name") or "HermesTool",
                "tool_input": tc,
            })
            task = asyncio.create_task(_bridge_permission(self.rpc, req_id, params, turn.conv_id, turn.loom_port))
            turn.permission_tasks.add(task)
            task.add_done_callback(turn.permission_tasks.discard)
            return
        if method.startswith("fs/") or method.startswith("terminal/"):
            await self.rpc.respond(req_id, error={"code": -32601, "message": "not supported by loom client"})
        else:
            await self.rpc.respond(req_id, result={})

    async def run_turn(
        self,
        prompt: str,
        *,
        conv_id: int,
        model: str | None,
        cwd: str,
        loom_port: int,
        branch: list[dict] | None,
        resume_session_id: str | None,
        fork_session: bool,
    ) -> AsyncGenerator[dict, None]:
        import time as _time

        async with self._lock:
            await self.ensure_started()
            assert self.rpc is not None
            assert self.proc is not None

            work_dir = cwd if (cwd and os.path.isdir(cwd)) else os.getcwd()
            acp_cwd = str(Path(work_dir)).replace("\\", "/")
            turn = _HermesTurn(conv_id, loom_port)
            self._active_turn = turn
            t0 = _time.time()
            session_id = ""
            prompt_task: asyncio.Task | None = None
            try:
                models_info = {}
                if resume_session_id:
                    try:
                        if fork_session:
                            log.info("[Hermes] Forking session: %s", resume_session_id)
                            fork_result = await self.rpc.request(
                                "session/fork",
                                {"sessionId": resume_session_id, "cwd": acp_cwd},
                                timeout=60.0,
                            )
                            session_id = (fork_result or {}).get("sessionId") or (fork_result or {}).get("session_id") or ""
                            models_info = (fork_result or {}).get("models") or {}
                        else:
                            log.info("[Hermes] Loading session: %s", resume_session_id)
                            load_result = await self.rpc.request(
                                "session/load",
                                {"sessionId": resume_session_id, "cwd": acp_cwd},
                                timeout=60.0,
                            )
                            session_id = resume_session_id
                            models_info = (load_result or {}).get("models") or {}
                    except Exception as e:
                        log.warning("[Hermes] Session resume/fork failed: %s", e)
                        await self.stop(remove=True)
                        raise RuntimeError(f"Hermes session resume/fork failed: {e}") from e

                if not session_id:
                    new_result = await self.rpc.request(
                        "session/new",
                        {"cwd": acp_cwd, "mcpServers": _loom_mcp_servers()},
                        timeout=60.0,
                    )
                    session_id = (new_result or {}).get("sessionId") or (new_result or {}).get("session_id") or ""
                    models_info = (new_result or {}).get("models") or {}

                yield {"type": "session_info", "session_id": session_id,
                       "model": models_info.get("currentModelId", "")}

                target_model_id = _loom_model_to_hermes(model)
                # session/set_model triggers a full agent re-init in Hermes
                # (auxiliary auto-detect + context-length probe), so skip it
                # when the forked/loaded session already runs the target model.
                current_model_id = (models_info or {}).get("currentModelId") or ""
                _norm = lambda m: m.split(":", 1)[1] if m.startswith("custom:") else m  # noqa: E731
                if target_model_id and _norm(target_model_id) != _norm(current_model_id):
                    try:
                        await self.rpc.request(
                            "session/set_model",
                            {"sessionId": session_id, "modelId": target_model_id},
                            timeout=60.0,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning("[Hermes] set_model(%s) failed: %s", target_model_id, e)

                prompt_blocks: list[dict] = [{"type": "text", "text": prompt}]
                prompt_blocks.extend(_collect_current_turn_image_blocks(branch))
                prompt_task = asyncio.create_task(self.rpc.request(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": prompt_blocks},
                    timeout=None,
                ))

                while True:
                    event_task = asyncio.create_task(turn.queue.get())
                    done, pending = await asyncio.wait(
                        {prompt_task, event_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if event_task in done:
                        evt = event_task.result()
                        yield evt
                    else:
                        event_task.cancel()

                    if prompt_task in done:
                        while not turn.queue.empty():
                            yield turn.queue.get_nowait()
                        prompt_failed = False
                        try:
                            result = prompt_task.result()
                        except Exception as e:  # noqa: BLE001
                            yield {"type": "error", "error": f"hermes prompt failed: {e}"}
                            result = {}
                            prompt_failed = True
                        usage = (result or {}).get("usage") or {}
                        if usage:
                            yield {
                                "type": "usage",
                                "input_tokens": usage.get("inputTokens", usage.get("input_tokens", 0)),
                                "output_tokens": usage.get("outputTokens", usage.get("output_tokens", 0)),
                            }
                        yield {
                            "type": "result",
                            "session_id": session_id,
                            "stop_reason": (
                                (result or {}).get("stopReason")
                                or (result or {}).get("stop_reason")
                                or ("error" if prompt_failed else "end_turn")
                            ),
                            "duration_ms": int((_time.time() - t0) * 1000),
                            "num_turns": 1,
                        }
                        break
            except asyncio.CancelledError:
                if prompt_task is not None:
                    prompt_task.cancel()
                await self.stop(remove=True)
                raise
            finally:
                if self._active_turn is turn:
                    self._active_turn = None
                for task in list(turn.permission_tasks):
                    task.cancel()

    async def stop(self, *, remove: bool = True) -> None:
        proc = self.proc
        self.proc = None
        self.rpc = None
        self._active_turn = None
        if remove:
            for key, runtime in list(_RUNTIMES.items()):
                if runtime is self:
                    _RUNTIMES.pop(key, None)
        if proc is not None:
            _PROC_RUNTIMES.pop(proc.pid, None)
            if proc.returncode is None:
                if sys.platform == "win32" and isinstance(proc, asyncio.subprocess.Process):
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
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self._reader_task = None
        self._stderr_task = None


_RUNTIMES: dict[tuple[int, int, str, str], _HermesAcpRuntime] = {}
_PROC_RUNTIMES: dict[int, _HermesAcpRuntime] = {}


def _runtime_key(exe: str, home: str) -> tuple[int, int, str, str]:
    # Include the event loop and subprocess factory identity so tests that
    # monkeypatch asyncio.create_subprocess_exec do not accidentally share a
    # runtime across fake processes.
    return (
        id(asyncio.get_running_loop()),
        id(asyncio.create_subprocess_exec),
        os.path.abspath(home),
        exe,
    )


async def shutdown_hermes_runtimes() -> None:
    for runtime in list(_RUNTIMES.values()):
        await runtime.stop(remove=True)


def find_runtime_by_home(home: str) -> "_HermesAcpRuntime | None":
    """Return the held runtime whose home matches `home`, or None.

    Used by the attendant-clear-on-model-stop binding (admin_server llama/dream
    stop paths) and the admin Hermes-status endpoint. Home is the identity key:
    the llama attendant's home is config.hermes_home, the dream attendant's is
    its -dream home, Prometheus' is its -prometheus home.
    """
    target = os.path.abspath(home)
    for runtime in _RUNTIMES.values():
        if os.path.abspath(runtime.home) == target:
            return runtime
    return None


def list_hermes_runtimes() -> list[dict]:
    """Snapshot of held runtimes for the admin Hermes-status endpoint.

    Read-only — never mutates _RUNTIMES. Reports home, alive state, and pid so
    the admin UI can show which of the three runtimes is warm without poking
    process tables.
    """
    out = []
    for runtime in _RUNTIMES.values():
        proc = runtime.proc
        out.append({
            "home": runtime.home,
            "alive": runtime._is_alive(),
            "pid": proc.pid if proc is not None else None,
            "agent_info": runtime._agent_info,
        })
    return out


async def stop_runtime_by_home(home: str) -> bool:
    """Stop the held runtime whose home matches `home`. Returns True if stopped.

    The attendant-clear-on-model-stop coupling: when a model server stops, this
    clears the bound attendant's warm process from _RUNTIMES. The soul (home dir,
    state.db, memories) survives; only the warm process is lost. On next attendant
    turn, _RUNTIMES misses and run_hermes re-inits (probe cache skips the
    expensive context-length probe).

    SAFETY: refuses to stop a runtime with an active turn (runtime._active_turn is
    not None). run_turn() holds _lock for the duration of a turn and sets
    _active_turn; stop() does NOT take _lock, so killing mid-turn would terminate
    the process backing a live generation — corrupting the user's conversation.
    A backend flip / model-stop that hits a live turn returns False and lets the
    turn finish against its current (already-spawned) process; the next turn
    re-routes. This is correct for both attendants (model-stop mid-turn is rare
    and the soul survives regardless) and Prometheus (the rewritten config.yaml
    takes effect on the next init regardless).
    """
    runtime = find_runtime_by_home(home)
    if runtime is None:
        return False
    if runtime._active_turn is not None:
        log.warning("[Hermes] refusing to stop runtime (home=%s) — active turn in flight",
                    runtime.home)
        return False
    await runtime.stop(remove=True)
    return True


async def reload_prometheus_backend(home: str) -> bool:
    """Clear Prometheus' warm runtime so the next turn re-inits against new config.

    The Prometheus router rewrites config.yaml (base_url + default model + api_key)
    on a backend change, then calls this. We do NOT session/set_model mid-flight:
    that RPC is per-session (hermes_client.py:817-821) and Hermes reads base_url at
    process init, not on set_model — so a warm process wouldn't actually move to
    the new endpoint. Instead, stop the warm process (soul-free: Prometheus has
    none) and let the next Prometheus turn lazy-init via run_hermes. The probe
    cache (seeded for both local + cloud by _ensure_prometheus_home) makes that
    re-init skip the 6.5s context-length probe.

    Returns True if a runtime was cleared, False if Prometheus wasn't warm
    (nothing to reload — the next turn will init against the new config directly).
    """
    return await stop_runtime_by_home(home)



# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

async def _run_hermes_oneshot(
    prompt: str,
    *,
    conv_id: int = 0,
    model: str | None = None,
    cwd: str = ".",
    loom_port: int = 3000,
    hermes_exe: str | None = None,
    hermes_home: str | None = None,
    system: str | None = None,
    branch: list[dict] | None = None,
    resume_session_id: str | None = None,
    fork_session: bool = False,
    is_first_turn: bool = True,
):
    """Spawn `hermes acp`, run one prompt turn, and return ``(proc, event_stream)``.

    Args:
        prompt: the fully-rendered user turn (caller bakes in history + persona).
        conv_id: Loom conversation id (for the permission bridge POST body).
        model: optional llama model name; if given, sent via ACP `session/set_model`
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
    prompt = _prepare_hermes_prompt(prompt, branch, model, is_first_turn=is_first_turn)
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

            session_id = ""
            models_info = {}
            if resume_session_id:
                try:
                    if fork_session:
                        log.info("[Hermes] Forking session: %s", resume_session_id)
                        fork_result = await rpc_request_via_reader(
                            rpc, "session/fork", {"sessionId": resume_session_id, "cwd": acp_cwd}, proc, state)
                        session_id = (fork_result or {}).get("sessionId") or (fork_result or {}).get("session_id") or ""
                        models_info = (fork_result or {}).get("models") or {}
                    else:
                        log.info("[Hermes] Loading session: %s", resume_session_id)
                        load_result = await rpc_request_via_reader(
                            rpc, "session/load", {"sessionId": resume_session_id, "cwd": acp_cwd}, proc, state)
                        session_id = resume_session_id
                        models_info = (load_result or {}).get("models") or {}
                except Exception as e:
                    log.warning("[Hermes] Session resume/fork failed: %s", e)
                    try:
                        await cancel_hermes(proc)
                    except Exception:
                        pass
                    raise RuntimeError(f"Hermes session resume/fork failed: {e}") from e

            if not session_id:
                new_result = await rpc_request_via_reader(
                    rpc, "session/new", {"cwd": acp_cwd, "mcpServers": _loom_mcp_servers()}, proc, state)
                session_id = (new_result or {}).get("sessionId") or (new_result or {}).get("session_id") or ""
                models_info = (new_result or {}).get("models") or {}

            yield {"type": "session_info", "session_id": session_id,
                   "model": models_info.get("currentModelId", "")}

            target_model_id = _loom_model_to_hermes(model)
            if target_model_id:
                try:
                    await rpc_request_via_reader(
                        rpc, "session/set_model",
                        {"sessionId": session_id, "modelId": target_model_id}, proc, state)
                except Exception as e:  # noqa: BLE001
                    log.warning("[Hermes] set_model(%s) failed: %s", target_model_id, e)

            # --- prompt turn ---
            # Issue the prompt request, then drain until its response arrives.
            prompt_req_id = rpc._alloc_id()
            prompt_fut: asyncio.Future = asyncio.get_event_loop().create_future()
            rpc._pending[prompt_req_id] = prompt_fut
            prompt_blocks: list[dict] = [{"type": "text", "text": prompt}]
            prompt_blocks.extend(_collect_current_turn_image_blocks(branch))
            await rpc._write({"jsonrpc": "2.0", "id": prompt_req_id, "method": "session/prompt",
                              "params": {"sessionId": session_id,
                                         "prompt": prompt_blocks}})

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
                        prompt_failed = False
                        try:
                            result = prompt_fut.result()
                        except Exception as e:  # noqa: BLE001
                            yield {"type": "error", "error": f"hermes prompt failed: {e}"}
                            result = {}
                            prompt_failed = True
                        usage = (result or {}).get("usage") or {}
                        if usage:
                            yield {"type": "usage",
                                   "input_tokens": usage.get("inputTokens", usage.get("input_tokens", 0)),
                                   "output_tokens": usage.get("outputTokens", usage.get("output_tokens", 0))}
                        yield {"type": "result",
                               "session_id": session_id,
                               "stop_reason": (
                                   (result or {}).get("stopReason")
                                   or (result or {}).get("stop_reason")
                                   or ("error" if prompt_failed else "end_turn")
                               ),
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


async def run_hermes(
    prompt: str,
    *,
    conv_id: int = 0,
    model: str | None = None,
    cwd: str = ".",
    loom_port: int = 3000,
    hermes_exe: str | None = None,
    hermes_home: str | None = None,
    system: str | None = None,
    branch: list[dict] | None = None,
    resume_session_id: str | None = None,
    fork_session: bool = False,
    is_first_turn: bool = True,
):
    """Run one prompt turn through a persistent `hermes acp` runtime."""
    del system  # reserved for parity with other provider clients
    prompt = _prepare_hermes_prompt(prompt, branch, model, is_first_turn=is_first_turn)
    exe = hermes_exe or default_hermes_exe(hermes_home)
    home = hermes_home or os.environ.get(
        "HERMES_HOME",
        os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes"),
    )
    key = _runtime_key(exe, home)
    runtime = _RUNTIMES.get(key)
    if runtime is None:
        runtime = _HermesAcpRuntime(exe=exe, home=home, cwd=cwd)
        _RUNTIMES[key] = runtime
    await runtime.ensure_started()
    assert runtime.proc is not None
    return runtime.proc, runtime.run_turn(
        prompt,
        conv_id=conv_id,
        model=model,
        cwd=cwd,
        loom_port=loom_port,
        branch=branch,
        resume_session_id=resume_session_id,
        fork_session=fork_session,
    )


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
    runtime = _PROC_RUNTIMES.get(getattr(proc, "pid", -1))
    if runtime is not None:
        await runtime.stop(remove=True)
        return
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
