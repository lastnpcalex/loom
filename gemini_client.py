"""Gemini Code CLI subprocess wrapper with NDJSON stream parser."""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Use the standard Loom blocking hook
_HOOK_SCRIPT = str(Path(__file__).parent / "cc_permission_hook.py")


def _process_event(raw: dict) -> list[dict]:
    """Process a raw Gemini stream-json event and return simplified event dicts."""
    events = []
    etype = raw.get("type", "")

    if "error" in raw:
        err = raw["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        events.append({"type": "result", "is_error": True, "error": msg})
        return events

    if etype == "init":
        events.append({
            "type": "session_info",
            "session_id": raw.get("session_id", ""),
            "model": raw.get("model", ""),
        })
    elif etype == "message":
        role = raw.get("role", "")
        content = raw.get("content", "")
        if role == "assistant" and content:
            events.append({"type": "text_delta", "text": content})
    elif etype == "tool_use":
        events.append({
            "type": "tool_start",
            "name": raw.get("tool_name", ""),
            "tool_id": raw.get("tool_id", ""),
        })
        params = raw.get("parameters", {})
        if params:
            events.append({
                "type": "tool_input_delta",
                "json": json.dumps(params, indent=2),
                "tool_id": raw.get("tool_id", ""),
            })
    elif etype == "tool_result":
        events.append({
            "type": "tool_result",
            "content": str(raw.get("output", "")),
            "tool_id": raw.get("tool_id", ""),
            "is_error": raw.get("status") != "success",
        })
    elif etype == "result":
        stats = raw.get("stats", {})
        events.append({"type": "usage", "input_tokens": stats.get("input_tokens", 0), "output_tokens": stats.get("output_tokens", 0)})
        events.append({
            "type": "result",
            "duration_ms": stats.get("duration_ms", 0),
            "result_text": raw.get("result", ""),
            "session_id": raw.get("session_id", ""),
            "is_error": raw.get("status") != "success",
        })
    return events


def _configure_permission_hook(cwd: str, backstage_parent_id: int | None = None, server_port: int = 8000):
    """Configure BeforeTool hook + trust entry so Gemini CLI calls our permission hook."""
    gemini_dir = Path(cwd) / ".gemini"
    gemini_dir.mkdir(parents=True, exist_ok=True)

    python_exe = sys.executable
    hook_path = _HOOK_SCRIPT
    # Gemini CLI runs hooks via PowerShell on Windows — "exe" "arg" syntax fails.
    # Use & (call operator) to handle quoted paths correctly in PowerShell.
    if sys.platform == "win32":
        hook_command = f'& "{python_exe}" "{hook_path}"'
    else:
        hook_command = f'"{python_exe}" "{hook_path}"'

    hook_def = [
        {
            "matcher": "*",
            "hooks": [{
                "type": "command",
                "command": hook_command,
                "timeout": 900000,  # 15 min — user needs time to approve in Loom UI
            }]
        }
    ]

    # Detect protocol (HTTPS if certs exist). Resolve relative to this script
    # — the server's cwd may differ from the project root (e.g. worktree),
    # which silently flips this to http:// and breaks the backstage MCP.
    _certs_dir = Path(__file__).parent / "certs"
    protocol = "https" if (_certs_dir / "cert.pem").exists() and (_certs_dir / "key.pem").exists() else "http"

    # Write project-level settings.json (the one Gemini actually reads)
    settings_path = gemini_dir / "settings.json"
    existing = {}
    if settings_path.exists():
        try: existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except: existing = {}

    existing["hooks"] = {"BeforeTool": hook_def}
    
    # Backstage: Inject MCP server for state cards
    if backstage_parent_id:
        existing["mcpServers"] = existing.get("mcpServers", {})
        existing["mcpServers"]["loom-state-cards"] = {
            "command": python_exe,
            "args": [str(Path(__file__).parent / "mcp_state_cards.py")],
            "env": {
                "LOOM_API_URL": f"{protocol}://127.0.0.1:{server_port}",
                "LOOM_BACKSTAGE_PARENT_ID": str(backstage_parent_id)
            }
        }
    
    settings_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    log.info(f"[GEMINI] Hook configured: {settings_path}")

    # Remove settings.local.json if it has stale Claude-style PreToolUse hooks
    local_path = gemini_dir / "settings.local.json"
    if local_path.exists():
        try:
            local = json.loads(local_path.read_text(encoding="utf-8"))
            if "PreToolUse" in local.get("hooks", {}):
                del local["hooks"]["PreToolUse"]
                local_path.write_text(json.dumps(local, indent=2), encoding="utf-8")
        except: pass

    # Add hook command to ~/.gemini/trusted_hooks.json so Gemini doesn't skip it
    _ensure_hook_trusted(cwd, hook_command)


def _ensure_hook_trusted(cwd: str, hook_command: str):
    """Add our hook command to Gemini's trusted_hooks.json for this project path."""
    if sys.platform == "win32":
        home = Path(os.environ.get("USERPROFILE", Path.home()))
    else:
        home = Path.home()
    trusted_path = home / ".gemini" / "trusted_hooks.json"

    trusted = {}
    if trusted_path.exists():
        try: trusted = json.loads(trusted_path.read_text(encoding="utf-8"))
        except: trusted = {}

    # Gemini stores trust entries with a colon prefix on the command string
    trust_key = f":{hook_command}"
    # Normalize the cwd to match Gemini's path format
    norm_cwd = str(Path(cwd).resolve())

    project_hooks = trusted.get(norm_cwd, [])
    if trust_key not in project_hooks:
        project_hooks.append(trust_key)
        trusted[norm_cwd] = project_hooks
        trusted_path.parent.mkdir(parents=True, exist_ok=True)
        trusted_path.write_text(json.dumps(trusted, indent=2), encoding="utf-8")
        log.info(f"[GEMINI] Hook trusted for {norm_cwd}")


async def run_gemini(prompt: str, cwd: str, conv_id: int = 0, server_port: int = 8000,
                     model: str = "sonnet", effort: str = "high",
                     permission_mode: str = "default",
                     resume_session_id: str = None, fork_session: bool = False,
                     backstage_parent_id: int | None = None):
    _configure_permission_hook(cwd, backstage_parent_id, server_port)

    # Use YOLO mode to ensure tools are visible in headless mode.
    # Security is enforced by our hook script which fires even in YOLO mode.
    cc_args = [
        "--model", model,
        "--output-format", "stream-json",
        "--approval-mode", "yolo",
        # Gemini CLI's "trusted folders" check otherwise overrides yolo back to
        # default and aborts headless runs from arbitrary cwds.
        "--skip-trust",
    ]
    if resume_session_id:
        cc_args.extend(["--resume", resume_session_id])

    # Backstage Lockdown: Use temporary policy to block file/shell tools
    if backstage_parent_id:
        policy_path = Path(cwd) / ".gemini" / "backstage_policy.toml"
        policy_content = """
[[rule]]
toolName = "*"
decision = "deny"
priority = 0

[[rule]]
mcpServerName = "loom-state-cards"
decision = "allow"
priority = 1000

[[rule]]
toolName = "list_directory"
decision = "allow"
priority = 1000

[[rule]]
toolName = "read_file"
decision = "allow"
priority = 1000
"""
        policy_path.write_text(policy_content.strip(), encoding="utf-8")
        cc_args.extend(["--policy", str(policy_path)])
        cc_args.extend(["--allowed-mcp-server-names", "loom-state-cards"])
    
    gemini_exe = "gemini.cmd" if sys.platform == "win32" else "gemini"

    env = {
        **os.environ,
        "LOOM_CONV_ID": str(conv_id),
        "LOOM_PORT": str(server_port),
        # Belt-and-suspenders for older Gemini CLI builds that ignore --skip-trust.
        "GEMINI_CLI_TRUST_WORKSPACE": "true",
    }
    if backstage_parent_id:
        env["LOOM_BACKSTAGE_PARENT_ID"] = str(backstage_parent_id)

    # Always pipe prompt via stdin on Windows — newlines in command-line args
    # get mangled by CreateProcess, causing Gemini to see only the first line.
    use_stdin = sys.platform == "win32" or len(prompt) > 20000
    if use_stdin:
        cc_args.extend(["-p", "-"])  # read prompt from stdin
    else:
        cc_args.extend(["-p", prompt])

    cmd = [gemini_exe] + cc_args

    print(f"[GEMINI] Prompt length: {len(prompt)} chars (stdin={use_stdin})")
    print(f"[GEMINI] Prompt preview: {repr(prompt[:200])}")

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdin=asyncio.subprocess.PIPE if use_stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024
    )

    # Feed prompt via stdin if needed in a background task to prevent pipe deadlocks
    if use_stdin and proc.stdin:
        async def _feed_stdin():
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            except Exception as e:
                log.error(f"[GEMINI] Error feeding stdin: {e}")
        asyncio.create_task(_feed_stdin())

    async def _read_stderr():
        async for line in proc.stderr:
            text = line.decode("utf-8", errors="replace").strip()
            if text: print(f"[GEMINI-stderr] {text}")
    asyncio.create_task(_read_stderr())

    async def _event_stream():
        async for line in proc.stdout:
            line = line.strip()
            if not line: continue
            try:
                raw = json.loads(line.decode("utf-8", errors="replace"))
                for evt in _process_event(raw): yield evt
                if raw.get("type") == "result": break
            except: continue
        await proc.wait()

    return proc, _event_stream()

async def cancel_gemini(proc):
    if proc.returncode is None:
        if sys.platform == 'win32':
            import subprocess
            try: subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True, timeout=5)
            except: proc.kill()
        else:
            proc.terminate()
        try: await asyncio.wait_for(proc.wait(), timeout=5)
        except: proc.kill()
