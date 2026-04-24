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


def _process_event(raw: dict) -> list[dict]:
    """Process a raw CC stream-json event and return simplified event dicts.

    CC's stream-json format emits top-level NDJSON events:
      - system: session info (session_id, cwd, model, tools)
      - assistant: full message with content blocks (text, tool_use, thinking)
      - tool_result: tool output
      - result: turn complete (duration, final text)
    """
    events = []
    etype = raw.get("type", "")

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

    elif etype == "assistant":
        message = raw.get("message", {})
        content = message.get("content", [])

        # content can be a string or a list of blocks
        if isinstance(content, str):
            if content:
                events.append({"type": "text_delta", "text": content})
        elif isinstance(content, list):
            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    text = block.get("text", "")
                    if text:
                        events.append({"type": "text_delta", "text": text})
                elif btype == "tool_use":
                    tool_id = block.get("id", "")
                    tool_name = block.get("name", "")
                    input_data = block.get("input", {})

                    events.append({
                        "type": "tool_start",
                        "name": tool_name,
                        "tool_id": tool_id,
                    })
                    if input_data:
                        events.append({
                            "type": "tool_input_delta",
                            "json": json.dumps(input_data, indent=2),
                            "tool_id": tool_id,
                        })

                    # Emit structured events for interactive tools
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
                elif btype == "thinking":
                    thinking = block.get("thinking", "")
                    if thinking:
                        events.append({"type": "thinking_delta", "text": thinking})

        # Extract usage if present
        usage = message.get("usage", {})
        if usage:
            # input_tokens only counts non-cached tokens; add cache reads for the true total
            input_tok = (usage.get("input_tokens", 0)
                         + usage.get("cache_read_input_tokens", 0)
                         + usage.get("cache_creation_input_tokens", 0))
            events.append({
                "type": "usage",
                "input_tokens": input_tok,
                "output_tokens": usage.get("output_tokens", 0),
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
        pass  # Ignore rate limit events silently

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
                     use_ollama: bool = False,
                     backstage_parent_id: int | None = None):
    """Run Claude Code CLI and yield parsed events as an async generator.

    Returns (process, generator) so the caller can cancel via process.terminate().
    When resume_session_id is provided, uses --resume to continue an existing session.
    When fork_session is True (with --resume), creates a new branch from that session.
    When use_ollama is True, launches via 'ollama launch claude --model <model> --yes --'
    so that Claude Code runs against a local Ollama model.
    Permission hooks route tool approvals through Loom's HTTP API.
    """
    # Configure the permission hook in the project directory (idempotent, skips if already set)
    # Skip on resume since the original session already configured the hook
    _configure_permission_hook(cwd) if not resume_session_id else None

    # Build the Claude Code arguments (common to both launch methods)
    disallowed = "AskUserQuestion,WebSearch,WebFetch" if use_ollama else "AskUserQuestion"
    cc_args = ["-p", prompt,
               "--output-format", "stream-json",
               "--verbose",
               "--disallowedTools", disallowed]

    # Backstage: inject the state-cards MCP server scoped to the parent conv.
    # Inline JSON config — no temp file needed. The subprocess receives
    # LOOM_API_URL / LOOM_BACKSTAGE_PARENT_ID via the server entry's env.
    if backstage_parent_id:
        mcp_script = str(Path(__file__).parent / "mcp_state_cards.py")
        mcp_config = {
            "mcpServers": {
                "loom-state-cards": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": [mcp_script],
                    "env": {
                        "LOOM_API_URL": f"http://127.0.0.1:{server_port}",
                        "LOOM_BACKSTAGE_PARENT_ID": str(backstage_parent_id),
                    },
                }
            }
        }
        cc_args.extend(["--mcp-config", json.dumps(mcp_config)])

    if not use_ollama:
        # Direct claude launch — model and effort are CC flags
        cc_args.extend(["--model", model, "--effort", effort])

    if permission_mode and permission_mode != "default":
        cc_args.extend(["--permission-mode", permission_mode])

    if resume_session_id:
        cc_args.extend(["--resume", resume_session_id])
        if fork_session:
            cc_args.append("--fork-session")

    if use_ollama:
        # Launch via: ollama launch claude --model <model> -- <cc_args>
        cmd = ["ollama", "launch", "claude", "--model", model, "--"] + cc_args
    else:
        cmd = ["claude"] + cc_args

    # Pass Loom connection info to the hook script via env vars
    env = {**os.environ}
    env["LOOM_CONV_ID"] = str(conv_id)
    env["LOOM_PORT"] = str(server_port)
    # Explicitly control CLAUDECODE so the launch method matches use_ollama.
    # When True: ollama launch claude needs CLAUDECODE=1 in its subprocess.
    # When False: plain claude must NOT see CLAUDECODE or it routes to Ollama.
    if use_ollama:
        env["CLAUDECODE"] = "1"
        env["CLAUDE_CODE_ENTRYPOINT"] = "cli"
    else:
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)

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
        if use_ollama:
            cmd = ["ollama", "launch", "claude", "--model", model, "--"] + cc_args
        else:
            cmd = ["claude"] + cc_args

    print(f"[CC] Starting subprocess in {cwd}")
    print(f"[CC] CMD: {' '.join(cmd[:8])}{'...' if len(cmd) > 8 else ''}")
    print(f"[CC] use_ollama={use_ollama} model={model} effort={effort}")
    print(f"[CC] Prompt length: {len(prompt)} chars (stdin={use_stdin})")
    print(f"[CC] Hook env: LOOM_CONV_ID={conv_id} LOOM_PORT={server_port}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if use_stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,  # 16 MB line buffer (CC can emit large base64/tool results)
    )

    # Feed prompt via stdin if needed
    if use_stdin and proc.stdin:
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()

    print(f"[CC] Process started, pid={proc.pid}")

    # Read stderr in background for debugging
    async def _read_stderr():
        async for line in proc.stderr:
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                print(f"[CC-stderr] {text}")

    asyncio.create_task(_read_stderr())

    async def _event_stream():
        async for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                print(f"[CC] Non-JSON line: {line[:200]}")
                continue

            rtype = raw.get("type", "?")
            print(f"[CC] event: {rtype}")

            for evt in _process_event(raw):
                yield evt

            # `result` is the final event — stop reading so we don't hang
            # if a background process (e.g. a server) inherited stdout
            if rtype == "result":
                print("[CC] Got result event, stopping stream reader")
                break

        # Wait for process exit with timeout — if a spawned server holds
        # the process tree open, don't block forever
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=10)
            print(f"[CC] Process exited with code {rc}")
        except asyncio.TimeoutError:
            print("[CC] Process didn't exit within 10s (likely spawned background server), terminating")
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

    return proc, _event_stream()


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
