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
                    events.append({
                        "type": "tool_start",
                        "name": block.get("name", ""),
                        "tool_id": tool_id,
                    })
                    # Include the input as formatted JSON
                    input_data = block.get("input", {})
                    if input_data:
                        events.append({
                            "type": "tool_input_delta",
                            "json": json.dumps(input_data, indent=2),
                            "tool_id": tool_id,
                        })
                elif btype == "thinking":
                    thinking = block.get("thinking", "")
                    if thinking:
                        events.append({"type": "thinking_delta", "text": thinking})

        # Extract usage if present
        usage = message.get("usage", {})
        if usage:
            events.append({
                "type": "usage",
                "input_tokens": usage.get("input_tokens", 0),
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
                    })

    elif etype == "result":
        events.append({
            "type": "result",
            "cost_usd": raw.get("cost_usd", 0),
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


def _configure_permission_hook(cwd: str):
    """Write a PermissionRequest hook to the project's .claude/settings.local.json.

    The hook routes permission requests through Loom's HTTP API so the user
    can approve/deny them in the browser UI.
    """
    claude_dir = Path(cwd) / ".claude"
    settings_path = claude_dir / "settings.local.json"

    # Read existing settings (preserve other config)
    existing = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            existing = {}

    # Build hook command using the same Python interpreter running Loom
    python_exe = sys.executable.replace("\\", "/")
    hook_path = _HOOK_SCRIPT.replace("\\", "/")
    hook_command = f'"{python_exe}" "{hook_path}"'

    # Use PreToolUse (not PermissionRequest — that doesn't fire in -p mode)
    # Matcher ".*" catches all tools; nested hooks array is required
    existing.setdefault("hooks", {})
    existing["hooks"].pop("PermissionRequest", None)  # Remove old hook
    existing["hooks"]["PreToolUse"] = [
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

    # Write settings
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"[CC] Configured permission hook in {settings_path}")


async def run_claude(prompt: str, cwd: str, conv_id: int = 0, server_port: int = 8000,
                     model: str = "sonnet", effort: str = "high",
                     resume_session_id: str = None, fork_session: bool = False,
                     use_ollama: bool = False):
    """Run Claude Code CLI and yield parsed events as an async generator.

    Returns (process, generator) so the caller can cancel via process.terminate().
    When resume_session_id is provided, uses --resume to continue an existing session.
    When fork_session is True (with --resume), creates a new branch from that session.
    When use_ollama is True, launches via 'ollama launch claude --model <model> --yes --'
    so that Claude Code runs against a local Ollama model.
    Permission hooks route tool approvals through Loom's HTTP API.
    """
    # Configure the permission hook in the project directory
    _configure_permission_hook(cwd)

    # Build the Claude Code arguments (common to both launch methods)
    cc_args = ["-p", prompt,
               "--output-format", "stream-json",
               "--verbose"]

    if not use_ollama:
        # Direct claude launch — model and effort are CC flags
        cc_args.extend(["--model", model, "--effort", effort])

    if resume_session_id:
        cc_args.extend(["--resume", resume_session_id])
        if fork_session:
            cc_args.append("--fork-session")

    if use_ollama:
        # Launch via: ollama launch claude --model <model> --yes -- <cc_args>
        cmd = ["ollama", "launch", "claude", "--model", model, "--yes", "--"] + cc_args
    else:
        cmd = ["claude"] + cc_args

    # Pass Loom connection info to the hook script via env vars
    env = {**os.environ}
    env["LOOM_CONV_ID"] = str(conv_id)
    env["LOOM_PORT"] = str(server_port)

    print(f"[CC] Starting subprocess in {cwd}")
    print(f"[CC] Prompt length: {len(prompt)} chars")
    print(f"[CC] Hook env: LOOM_CONV_ID={conv_id} LOOM_PORT={server_port}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,  # 16 MB line buffer (CC can emit large base64/tool results)
    )

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
    """Kill a running Claude Code subprocess."""
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
