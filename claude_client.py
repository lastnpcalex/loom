"""Claude Code CLI subprocess wrapper with NDJSON stream parser."""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Absolute path to the hook script (same directory as this file)
_HOOK_SCRIPT = str(Path(__file__).parent / "cc_permission_hook.py")

# Cap auto-continue relaunches so a runaway model can't loop forever. Five
# rounds covers any realistic max_tokens trip without becoming a token bomb.
_AUTO_CONTINUE_MAX = 5
_AUTO_CONTINUE_PROMPT = (
    "Continue from where your previous response was cut off. "
    "Do not restate context, apologize, or repeat what you already wrote — "
    "just resume the next token."
)


class _CCHandle:
    """Proxy over the active CC subprocess so the caller's reference stays
    valid across auto-continue relaunches. server.py stores this once in
    `_active_claude_procs[conv_id]`; when the inner stream relaunches CC on
    a max_tokens stop, we swap the underlying proc and `.kill()` / `.pid`
    keep targeting whichever subprocess is currently streaming."""

    __slots__ = ("_proc", "_cancelled")

    def __init__(self, proc):
        self._proc = proc
        # Set when the caller cancels via kill()/terminate(). The relaunch
        # path checks this after spawning a new proc so a cancel that lands
        # during the spawn await doesn't orphan the just-started subprocess.
        self._cancelled = False

    def _swap(self, new_proc) -> bool:
        """Adopt new_proc as the active subprocess. Returns False (and kills
        new_proc) if a cancel landed during the spawn await — the caller
        should break out of the relaunch loop instead of streaming on."""
        if self._cancelled:
            try:
                new_proc.kill()
            except Exception:
                pass
            return False
        self._proc = new_proc
        return True

    @property
    def cancelled(self):
        return self._cancelled

    @property
    def pid(self):
        return self._proc.pid

    @property
    def returncode(self):
        return self._proc.returncode

    @property
    def stdin(self):
        return self._proc.stdin

    @property
    def stdout(self):
        return self._proc.stdout

    @property
    def stderr(self):
        return self._proc.stderr

    def kill(self):
        self._cancelled = True
        try:
            self._proc.kill()
        except Exception:
            pass

    def terminate(self):
        self._cancelled = True
        try:
            self._proc.terminate()
        except Exception:
            pass

    async def wait(self):
        return await self._proc.wait()


def _build_continue_cmd(orig_cmd: list, session_id: str, continue_prompt: str) -> list:
    """Mutate a CC command list for an auto-continue relaunch:
    - swap the -p prompt to the continue text (or replace stdin sentinel)
    - replace existing --resume value or append a new --resume session_id pair
    - drop --fork-session (we extend the same session, not branch from it)
    """
    new = list(orig_cmd)
    try:
        i = new.index("-p")
        if i + 1 < len(new):
            new[i + 1] = continue_prompt
    except ValueError:
        new.extend(["-p", continue_prompt])
    try:
        i = new.index("--resume")
        if i + 1 < len(new):
            new[i + 1] = session_id
        else:
            new.append(session_id)
    except ValueError:
        new.extend(["--resume", session_id])
    if "--fork-session" in new:
        new.remove("--fork-session")
    return new


def _extract_message_text(content) -> str:
    """Pull readable text out of a CC transcript record's message.content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                if t:
                    parts.append(t)
        return "\n\n".join(parts)
    return ""


async def read_compact_summary(session_id: str, timeout_sec: float = 8.0) -> str | None:
    """Return the narrative summary CC wrote after a compact, or None.

    Modern CC (2.x) opens a post-compact session transcript with a `type:"user"`
    record flagged `isCompactSummary: true` whose message content is the
    narrative summary ("This session is being continued from a previous
    conversation…"). Older CC versions wrote a separate `type:"summary"`
    record. We handle both. The transcript is written asynchronously so we
    retry with a short backoff before giving up.
    """
    if not session_id:
        return None
    projects = Path.home() / ".claude" / "projects"
    deadline = asyncio.get_event_loop().time() + timeout_sec
    delay = 0.25
    while True:
        # Glob across project dirs since the cwd-hash encoding isn't stable to
        # guess — cheaper than computing it and robust to future changes.
        candidates = list(projects.glob(f"*/{session_id}.jsonl"))
        for path in candidates:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("isCompactSummary"):
                            msg = rec.get("message") or {}
                            content = msg.get("content") if isinstance(msg, dict) else None
                            text = _extract_message_text(content).strip()
                            if text:
                                return text
                        if rec.get("type") == "summary":
                            msg = rec.get("message") or {}
                            content = msg.get("content") if isinstance(msg, dict) else None
                            text = _extract_message_text(content).strip()
                            if text:
                                return text
                            alt = rec.get("summary")
                            if isinstance(alt, str) and alt.strip():
                                return alt.strip()
            except OSError:
                continue
        if asyncio.get_event_loop().time() >= deadline:
            return None
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 1.0)


def _process_event(raw: dict, state: dict | None = None) -> list[dict]:
    """Process a raw CC stream-json event and return simplified event dicts.

    With --include-partial-messages, CC emits incremental Anthropic SSE-style
    `stream_event` records (content_block_start/_delta/_stop, message_delta,
    etc.) AS the response is being generated, plus a final `assistant` event
    at end-of-turn with the assembled content. We stream from the SSE deltas
    and skip content emission from `assistant` to avoid duplication.

    The `state` dict carries per-stream context across calls — primarily a
    map of content_block index -> {type, name, tool_id, input_json} so we
    can finalize tool calls (AskUserQuestion / ExitPlanMode) when their
    accumulated input JSON parses on content_block_stop.

    Top-level NDJSON events handled:
      - system: session info (session_id, cwd, model, tools)
      - stream_event: SSE-style incremental deltas (partial messages)
      - assistant: end-of-turn message; usage extracted, content suppressed
      - user / tool_result: tool output
      - result: turn complete (duration, final text)
    """
    if state is None:
        state = {}
    blocks = state.setdefault("blocks", {})

    events = []
    etype = raw.get("type", "")

    # Track the latest session_id from any event that carries one. CC mints a
    # new session_id at compact_boundary, so capturing only from the initial
    # `system` init event would leave the auto-continue loop --resume'ing the
    # wrong (pre-compact) session.
    sid_in_raw = raw.get("session_id")
    if sid_in_raw:
        state["session_id"] = sid_in_raw

    if etype == "system":
        subtype = raw.get("subtype", "")
        if subtype == "compact_boundary":
            # CC compactified its context window.
            # The detailed summary is baked into CC's next assistant response,
            # not available as a separate field in this event.
            meta = raw.get("compact_metadata", {})
            events.append({
                "type": "compact_boundary",
                "trigger": meta.get("trigger", "auto"),
                "pre_tokens": meta.get("pre_tokens"),
                "session_id": raw.get("session_id", ""),
            })
        elif subtype == "api_retry":
            events.append({
                "type": "api_retry",
                "attempt": raw.get("attempt"),
                "max_retries": raw.get("max_retries"),
                "retry_delay_ms": raw.get("retry_delay_ms"),
                "error": raw.get("error", ""),
            })
        else:
            events.append({
                "type": "session_info",
                "session_id": raw.get("session_id", ""),
                "model": raw.get("model", ""),
            })

    elif etype == "stream_event":
        # Anthropic SSE-style partial-message events. Map deltas to our
        # streaming events; track per-block state so tool input JSON can
        # be reassembled and parsed at content_block_stop.
        ev = raw.get("event", {})
        evt_type = ev.get("type", "")

        # message_delta carries the canonical per-API-call final usage and the
        # turn's stop_reason. With --include-partial-messages the `assistant`
        # event's usage is a stale snapshot from message_start (output_tokens
        # under-reported), so this is now our sole usage source.
        if evt_type == "message_delta":
            sr = (ev.get("delta") or {}).get("stop_reason")
            if sr:
                state["stop_reason"] = sr
            usage = ev.get("usage") or {}
            if usage:
                input_tok = (usage.get("input_tokens", 0)
                             + usage.get("cache_read_input_tokens", 0)
                             + usage.get("cache_creation_input_tokens", 0))
                events.append({
                    "type": "usage",
                    "input_tokens": input_tok,
                    "output_tokens": usage.get("output_tokens", 0),
                })

        if evt_type == "content_block_start":
            idx = ev.get("index", 0)
            block = ev.get("content_block", {})
            btype = block.get("type", "")
            if btype == "text":
                blocks[idx] = {"type": "text"}
            elif btype == "thinking":
                blocks[idx] = {"type": "thinking"}
            elif btype == "tool_use":
                tool_id = block.get("id", "")
                tool_name = block.get("name", "")
                blocks[idx] = {
                    "type": "tool_use",
                    "tool_id": tool_id,
                    "name": tool_name,
                    "input_json": "",
                }
                events.append({
                    "type": "tool_start",
                    "name": tool_name,
                    "tool_id": tool_id,
                })

        elif evt_type == "content_block_delta":
            idx = ev.get("index", 0)
            delta = ev.get("delta", {})
            dtype = delta.get("type", "")
            blk = blocks.get(idx) or {}
            if dtype == "text_delta":
                txt = delta.get("text", "")
                if txt:
                    events.append({"type": "text_delta", "text": txt})
            elif dtype == "thinking_delta":
                thinking = delta.get("thinking", "")
                if thinking:
                    events.append({"type": "thinking_delta", "text": thinking})
            elif dtype == "input_json_delta":
                pj = delta.get("partial_json", "")
                if blk.get("type") == "tool_use":
                    blk["input_json"] = blk.get("input_json", "") + pj
                events.append({
                    "type": "tool_input_delta",
                    "json": pj,
                    "tool_id": blk.get("tool_id", ""),
                })

        elif evt_type == "content_block_stop":
            idx = ev.get("index", 0)
            blk = blocks.get(idx) or {}
            if blk.get("type") == "tool_use":
                # Parse the accumulated input JSON for interactive tools.
                try:
                    input_data = json.loads(blk.get("input_json", "") or "{}")
                except Exception:
                    input_data = {}
                tool_name = blk.get("name", "")
                tool_id = blk.get("tool_id", "")
                if tool_name == "AskUserQuestion" and isinstance(input_data, dict):
                    events.append({
                        "type": "ask_user_question",
                        "questions": input_data.get("questions", []),
                        "tool_id": tool_id,
                    })
                elif tool_name == "ExitPlanMode" and isinstance(input_data, dict):
                    events.append({
                        "type": "plan_ready",
                        "plan": input_data.get("plan", ""),
                        "plan_file": input_data.get("planFilePath", ""),
                        "tool_id": tool_id,
                    })
            blocks.pop(idx, None)

    elif etype == "assistant":
        # With --include-partial-messages, content has already streamed via
        # stream_event deltas and canonical usage comes from message_delta.
        # Suppress both here to avoid duplicating content and double-counting
        # tokens (the assistant event's usage is a stale message_start snapshot).
        message = raw.get("message", {})
        # Detect CC-synthesized error messages (e.g. rate-limit 429s). CC
        # fabricates these client-side and the wording — "monthly usage limit"
        # for an hourly trip — is unreliable. Surface the diagnostic fields so
        # server.py can annotate the saved message instead of trusting the text.
        if message.get("model") == "<synthetic>" and raw.get("isApiErrorMessage"):
            events.append({
                "type": "cc_synthetic_error",
                "error": raw.get("error", ""),
                "status": raw.get("apiErrorStatus"),
            })

    elif etype == "tool_result":
        content = raw.get("content", "")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get("text", str(block)))
                else:
                    parts.append(str(block))
            content = "\n".join(parts)
        events.append({
            "type": "tool_result",
            "content": str(content),
            "tool_id": raw.get("tool_use_id", ""),
        })

    elif etype == "user":
        # CC sends tool results as "user" events with content blocks
        message = raw.get("message", {})
        content = message.get("content", [])
        if isinstance(content, list):
            for block in content:
                btype = block.get("type", "")
                if btype == "tool_result":
                    result_content = block.get("content", "")
                    # content can be a list of sub-blocks
                    if isinstance(result_content, list):
                        parts = []
                        for sub in result_content:
                            if isinstance(sub, dict):
                                parts.append(sub.get("text", str(sub)))
                            else:
                                parts.append(str(sub))
                        result_content = "\n".join(parts)
                    events.append({
                        "type": "tool_result",
                        "content": str(result_content),
                        "tool_id": block.get("tool_use_id", ""),
                        "is_error": block.get("is_error", False),
                    })

    elif etype == "result":
        events.append({
            "type": "result",
            "cost_usd": raw.get("total_cost_usd", raw.get("cost_usd", 0)),
            "duration_ms": raw.get("duration_ms", 0),
            "duration_api_ms": raw.get("duration_api_ms", 0),
            "num_turns": raw.get("num_turns", 1),
            "result_text": raw.get("result", ""),
            "session_id": raw.get("session_id", ""),
            "is_error": raw.get("is_error", False),
        })

    elif etype == "rate_limit_event":
        # Forward rate-limit details so server can surface accurate window/reset
        # info instead of trusting CC's verbatim error wording (which has
        # mislabeled hourly trips as "monthly").
        log.info("CC rate_limit_event: %s", json.dumps(raw, default=str)[:1000])
        events.append({
            "type": "rate_limit",
            "data": {k: v for k, v in raw.items() if k != "type"},
        })

    else:
        # Forward unknown events for debugging
        log.info("Unhandled CC event type=%s keys=%s", etype, list(raw.keys()))
        log.info("Event data: %s", json.dumps(raw, default=str)[:1000])
        events.append({
            "type": "cc_raw_event",
            "event_type": etype,
            "data": raw,
        })

    return events


def _configure_permission_hook(cwd: str) -> bool:
    """Write a PreToolUse hook to the project's .claude/settings.local.json.

    The hook routes permission requests through Loom's HTTP API so the user
    can approve/deny them in the browser UI.

    Returns True if file was written, False if already configured (idempotent).
    This avoids the ~2s file I/O delay on Windows when called on every turn.
    """
    claude_dir = Path(cwd) / ".claude"
    settings_path = claude_dir / "settings.local.json"

    # Build hook command using the same Python interpreter running Loom
    python_exe = sys.executable.replace("\\", "/")
    hook_path = _HOOK_SCRIPT.replace("\\", "/")
    hook_command = f'"{python_exe}" "{hook_path}"'

    # Use PreToolUse (not PermissionRequest — that doesn't fire in -p mode)
    # Matcher ".*" catches all tools; nested hooks array is required
    new_config = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": ".*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_command,
                        }
                    ]
                }
            ]
        }
    }

    # Read existing settings (preserve other config if file exists)
    existing = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            existing = {}

    # Only write if the hooks section differs (idempotent check)
    existing_hooks = existing.get("hooks", {})

    # Compare: skip write if PreToolUse exists and command is unchanged
    # The command is nested at PreToolUse[0].hooks[0].command
    if "PreToolUse" in existing_hooks:
        existing_item = existing_hooks["PreToolUse"][0]
        existing_cmd = existing_item.get("command", "") or existing_item.get("hooks", [{}])[0].get("command", "")
        if existing_cmd == hook_command:
            print(f"[CC] Skipping write: command unchanged")
            return False  # Already configured, skip write

    # Write settings
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(new_config, indent=2), encoding="utf-8")
    print(f"[CC] Hook configured/updated in {settings_path}")

    return True


async def run_claude(prompt: str, cwd: str, conv_id: int = 0, server_port: int = 8000,
                     model: str = "sonnet", effort: str = "high",
                     permission_mode: str = "default",
                     resume_session_id: str = None, fork_session: bool = False,
                     use_llama: bool = False,
                     backstage_parent_id: int | None = None,
                     extra_mcp_servers: dict | None = None,
                     extra_disallowed_tools: list[str] | None = None,
                     append_system_prompt: str | None = None,
                     extra_env: dict | None = None):
    """Run Claude Code CLI and yield parsed events as an async generator.

    Returns (process, generator) so the caller can cancel via process.terminate().
    When resume_session_id is provided, uses --resume to continue an existing session.
    When fork_session is True (with --resume), creates a new branch from that session.
    When use_llama is True, launches plain `claude` with ANTHROPIC_BASE_URL/auth/model
    env vars pointing at the local llama-server (which speaks Anthropic's /v1/messages
    natively on port 11434). The model filename (e.g. Qwen3.6-27B-NVFP4.gguf) is set
    as all three DEFAULT_*_MODEL env vars so CC routes to it regardless of preset.
    Permission hooks route tool approvals through Loom's HTTP API.
    """
    # Configure the permission hook in the project directory (idempotent, skips if already set)
    # Skip on resume since the original session already configured the hook
    _configure_permission_hook(cwd) if not resume_session_id else None

    # Build the Claude Code arguments
    disallowed_list = ["AskUserQuestion"]
    # Backstage: lock the agent down to state-card MCP tools only — no filesystem
    # or shell access, no sub-agents. Otherwise the agent treats the Loom repo
    # (its cwd) as a codebase to refactor instead of editing cards.
    if backstage_parent_id:
        disallowed_list += [
            "Read", "Write", "Edit", "NotebookEdit",
            "Bash", "Glob", "Grep", "Agent",
            "WebSearch", "WebFetch",
        ]
    # Llama/local models: block built-in WebSearch/WebFetch (require Anthropic API).
    # The MCP web-tools server (registered below) provides keyless web_search/web_fetch
    # via DuckDuckGo + trafilatura as the replacement.
    if use_llama:
        disallowed_list += ["WebSearch", "WebFetch"]
    if extra_disallowed_tools:
        disallowed_list += [tool for tool in extra_disallowed_tools if tool not in disallowed_list]
    cc_args = ["-p", prompt,
               "--output-format", "stream-json",
               "--include-partial-messages",
               "--verbose",
               "--disallowedTools", ",".join(disallowed_list)]

    # Detect protocol (HTTPS if certs exist). Resolve relative to this script
    # — the server's cwd may differ from the project root (e.g. worktree),
    # which silently flips this to http:// and breaks the backstage MCP.
    _certs_dir = Path(__file__).parent / "certs"
    protocol = "https" if (_certs_dir / "cert.pem").exists() and (_certs_dir / "key.pem").exists() else "http"

    # Backstage: inject the state-cards MCP server scoped to the parent conv.
    # Inline JSON config — no temp file needed. The subprocess receives
    # LOOM_API_URL / LOOM_BACKSTAGE_PARENT_ID via the server entry's env.
    mcp_servers: dict = {}
    if backstage_parent_id:
        mcp_script = str(Path(__file__).parent / "mcp_state_cards.py")
        mcp_servers["loom-state-cards"] = {
            "type": "stdio",
            "command": sys.executable,
            "args": [mcp_script],
            "env": {
                "LOOM_API_URL": f"{protocol}://127.0.0.1:{server_port}",
                "LOOM_BACKSTAGE_PARENT_ID": str(backstage_parent_id),
            },
        }

        # Append a backstage-specific system prompt so the agent knows it's
        # editing state cards for a parent RP conv, not assisting with code.
        backstage_md = Path(__file__).parent / "backstage.md"
        if backstage_md.exists():
            cc_args.extend(["--append-system-prompt", backstage_md.read_text(encoding="utf-8")])

    # Llama/local models: register MCP web-tools so they get web_search/web_fetch
    # via DuckDuckGo + trafilatura (keyless, no Anthropic API needed).
    if use_llama:
        web_tools_script = Path(__file__).parent / "mcp_web_tools.py"
        if web_tools_script.is_file():
            mcp_servers["web-tools"] = {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(web_tools_script)],
            }

    # NROL-AO: make the engine MCP available to ordinary Loom-launched CC
    # sessions, not just the admin scan worker. User-scope `claude mcp add`
    # still works for external Claude Code sessions; this inline config keeps
    # Loom self-contained.
    if os.environ.get("NROL_AO_AUTO_MCP", "1") not in {"0", "false", "False"}:
        nrol_repo = Path(os.environ.get("NROL_AO_REPO", r"C:\Claude-Code\NROL-AO\temp-repo"))
        nrol_server = Path(__file__).parent / "mcp_servers" / "nrol_ao" / "server.py"
        if nrol_repo.exists() and nrol_server.is_file() and "nrol-ao" not in mcp_servers:
            nrol_env = {
                "NROL_AO_REPO": str(nrol_repo),
                "NROL_AO_ACTIVITY_DIR": os.environ.get(
                    "NROL_AO_ACTIVITY_DIR",
                    str(nrol_repo / "loom" / "mcp_activity"),
                ),
                "ALPHA_OMEGA_PORT": os.environ.get("ALPHA_OMEGA_PORT", "8098"),
                "LOOM_PORT": str(server_port),
                "LOOM_CONV_ID": str(conv_id),
                "PYTHONPATH": str(Path(__file__).parent)
                + os.pathsep
                + os.environ.get("PYTHONPATH", ""),
            }
            try:
                from config import config as _loom_config
                nrol_env.setdefault("NROL_AO_LLAMA_HOST", _loom_config.llama_host_url())
                if getattr(_loom_config, "llama_model", ""):
                    nrol_env.setdefault("NROL_AO_LLAMA_MODEL", _loom_config.llama_model)
            except Exception:
                pass
            mcp_servers["nrol-ao"] = {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "mcp_servers.nrol_ao.server"],
                "env": nrol_env,
            }

    if extra_mcp_servers:
        mcp_servers.update(extra_mcp_servers)
    if mcp_servers:
        cc_args.extend(["--mcp-config", json.dumps({"mcpServers": mcp_servers})])
    if append_system_prompt:
        cc_args.extend(["--append-system-prompt", append_system_prompt])

    # Always launch plain claude — use_llama overrides via env vars below.
    cc_args.extend(["--model", model, "--effort", effort])

    if permission_mode and permission_mode != "default":
        cc_args.extend(["--permission-mode", permission_mode])

    if resume_session_id:
        cc_args.extend(["--resume", resume_session_id])
        if fork_session:
            cc_args.append("--fork-session")

    cmd = ["claude"] + cc_args

    # Pass Loom connection info to the hook script via env vars
    env = {**os.environ}
    env["LOOM_CONV_ID"] = str(conv_id)
    if backstage_parent_id:
        env["LOOM_BACKSTAGE_PARENT_ID"] = str(backstage_parent_id)
    env["LOOM_PORT"] = str(server_port)
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items() if v is not None})
    # Always clear ollama-style entrypoint flags — we never use ollama launch anymore.
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    # Llama Server as CC backend: point the Anthropic SDK at llama-server's
    # native /v1/messages endpoint. Both API_KEY and AUTH_TOKEN are set because
    # different SDK code paths read different env vars. The DEFAULT_*_MODEL vars
    # override CC's internal preset→API-id mapping so the .gguf filename routes
    # correctly regardless of which preset (sonnet/opus/haiku) the user picked.
    if use_llama:
        from config import config as _loom_config
        base = _loom_config.llama_host_url()
        alias = model  # e.g. "Qwen3.6-27B-NVFP4.gguf"
        env["ANTHROPIC_BASE_URL"] = base
        env["ANTHROPIC_API_KEY"] = "dummy"
        env["ANTHROPIC_AUTH_TOKEN"] = "dummy"
        env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = alias
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = alias
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = alias
        # Suppress the per-request attribution hash that defeats KV-cache prefix.
        env["CLAUDE_CODE_ATTRIBUTION_HEADER"] = "0"

    # If the prompt is too long for a command-line arg (Windows ~32K limit),
    # pipe it via stdin instead of -p
    use_stdin = len(prompt) > 20000
    if use_stdin:
        # Replace -p <prompt> with just -p and pipe via stdin
        # Claude Code reads from stdin when -p is given without a value,
        # but safer to use --pipe mode: remove -p and its arg, add -p -
        try:
            p_idx = cc_args.index("-p")
            cc_args.pop(p_idx)      # remove -p
            cc_args.pop(p_idx)      # remove the prompt value
            cc_args.insert(0, "-p")
            cc_args.insert(1, "-")  # read from stdin
        except (ValueError, IndexError):
            use_stdin = False

        # Rebuild cmd with updated cc_args
        cmd = ["claude"] + cc_args

    async def _spawn_cc(spawn_cmd: list, spawn_prompt: str | None, spawn_use_stdin: bool):
        """Launch a CC subprocess and start its stderr drainer. Returns the
        asyncio.subprocess.Process. Used for the initial run and each
        auto-continue relaunch on max_tokens stop."""
        kwargs = {}
        if sys.platform == "win32":
            import subprocess
            # Use CREATE_NO_WINDOW (0x08000000) and CREATE_NEW_PROCESS_GROUP (0x00000200)
            kwargs["creationflags"] = 0x08000000 | 0x00000200
        p = await asyncio.create_subprocess_exec(
            *spawn_cmd,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE if spawn_use_stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
            **kwargs
        )
        if spawn_use_stdin and p.stdin and spawn_prompt is not None:
            async def _feed_stdin():
                try:
                    p.stdin.write(spawn_prompt.encode("utf-8"))
                    await p.stdin.drain()
                    p.stdin.close()
                except Exception as e:
                    print(f"[CC] Error feeding stdin: {e}")
            asyncio.create_task(_feed_stdin())

        async def _read_stderr():
            async for line in p.stderr:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    print(f"[CC-stderr] {text}")
        asyncio.create_task(_read_stderr())
        return p

    print(f"[CC] Starting subprocess in {cwd}")
    print(f"[CC] CMD: {' '.join(cmd[:8])}{'...' if len(cmd) > 8 else ''}")
    print(f"[CC] use_llama={use_llama} model={model} effort={effort}")
    print(f"[CC] Prompt length: {len(prompt)} chars (stdin={use_stdin})")
    print(f"[CC] Hook env: LOOM_CONV_ID={conv_id} LOOM_PORT={server_port}")

    proc = await _spawn_cc(cmd, prompt, use_stdin)
    print(f"[CC] Process started, pid={proc.pid}")
    handle = _CCHandle(proc)

    async def _event_stream():
        # Per-stream state carried across _process_event calls — content_block
        # index map, captured session_id, captured stop_reason. Survives across
        # auto-continue relaunches so the second run knows which session_id
        # to --resume against and so accumulated tool_use blocks don't reset.
        stream_state: dict = {}
        relaunches = 0
        # Buffer the `result` event across relaunches: the caller treats it
        # as "stream done", so we only emit it after we've decided not to
        # relaunch. Cost / duration / num_turns get summed across rounds.
        pending_result: dict | None = None
        cum_cost_usd = 0.0
        cum_duration_ms = 0
        cum_turns = 0

        while True:
            active = handle._proc
            saw_result = False
            async for line in active.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    print(f"[CC] Non-JSON line: {line[:200]}")
                    continue

                rtype = raw.get("type", "?")
                if rtype == "stream_event":
                    _ev = raw.get("event", {}) or {}
                    _evt_type = _ev.get("type", "?")
                    _delta = _ev.get("delta", {}) or {}
                    _dtype = _delta.get("type", "")
                    _snippet = ""
                    if _dtype == "text_delta":
                        _snippet = repr((_delta.get("text") or "")[:40])
                    elif _dtype == "thinking_delta":
                        _snippet = repr((_delta.get("thinking") or "")[:40])
                    print(f"[CC-TRACE] stream_event {_evt_type}/{_dtype} {_snippet}")
                else:
                    print(f"[CC] event: {rtype}")

                for evt in _process_event(raw, stream_state):
                    et = evt.get("type")
                    if et == "result":
                        # Aggregate across relaunches; emit once at the end.
                        cum_cost_usd += float(evt.get("cost_usd") or 0)
                        cum_duration_ms += int(evt.get("duration_ms") or 0)
                        cum_turns += int(evt.get("num_turns") or 0)
                        merged = dict(evt)
                        merged["cost_usd"] = cum_cost_usd
                        merged["duration_ms"] = cum_duration_ms
                        merged["num_turns"] = cum_turns
                        pending_result = merged
                    elif et == "session_info" and relaunches > 0:
                        # Suppress the relaunch's session_info — the UI already
                        # has the session_id from the first invocation, and a
                        # second one mid-stream confuses the message-finalize path.
                        continue
                    else:
                        yield evt

                if rtype == "result":
                    saw_result = True
                    print("[CC] Got result event, stopping stream reader")
                    break

            # Reap the just-finished proc before deciding whether to relaunch.
            try:
                rc = await asyncio.wait_for(active.wait(), timeout=10)
                print(f"[CC] Process exited with code {rc}")
            except asyncio.TimeoutError:
                print("[CC] Process didn't exit within 10s (likely spawned background server), terminating")
                active.terminate()
                try:
                    await asyncio.wait_for(active.wait(), timeout=5)
                except asyncio.TimeoutError:
                    active.kill()

            sr = stream_state.get("stop_reason")
            sid = stream_state.get("session_id")
            should_continue = (
                saw_result
                and sr == "max_tokens"
                and bool(sid)
                and relaunches < _AUTO_CONTINUE_MAX
            )
            if not should_continue:
                if sr == "max_tokens" and relaunches >= _AUTO_CONTINUE_MAX:
                    print(f"[CC] Auto-continue cap ({_AUTO_CONTINUE_MAX}) reached; stopping")
                break

            relaunches += 1
            print(f"[CC] Auto-continue #{relaunches}: stop=max_tokens, sid={sid[:8]}...")
            # Surface a status banner immediately — without this the user sees
            # a 1–2s silent gap while CC respawns before the next text_delta.
            yield {"type": "auto_continue", "round": relaunches}
            # Reset per-round state so the next round can capture its own
            # stop_reason without inheriting "max_tokens" from this one.
            stream_state["stop_reason"] = None
            # Tool-use block index map is per-message; clear it so the
            # continuation's content_block indexes don't collide with the
            # truncated message's indexes.
            stream_state["blocks"] = {}

            cont_cmd = _build_continue_cmd(cmd, sid, _AUTO_CONTINUE_PROMPT)
            if handle.cancelled:
                # User cancelled while we were reaping the previous proc.
                print("[CC] Auto-continue aborted: cancel before relaunch")
                break
            try:
                new_proc = await _spawn_cc(cont_cmd, _AUTO_CONTINUE_PROMPT, False)
            except Exception as e:
                print(f"[CC] Auto-continue spawn failed: {e}")
                break
            print(f"[CC] Auto-continue subprocess started, pid={new_proc.pid}")
            if not handle._swap(new_proc):
                # Cancel landed during the spawn await; _swap killed new_proc.
                print("[CC] Auto-continue aborted: cancel during spawn")
                break
            # Loop back to drain the new proc's stdout.

        if pending_result is not None:
            yield pending_result

    return handle, _event_stream()


async def cancel_claude(proc):
    """Kill a running Claude Code subprocess and its process tree."""
    if proc.returncode is None:
        import sys
        if sys.platform == 'win32':
            # On Windows, terminate() only kills the top process.
            # Use taskkill /T to kill the entire process tree.
            import subprocess
            try:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                               capture_output=True, timeout=5)
            except Exception:
                proc.kill()
        else:
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
