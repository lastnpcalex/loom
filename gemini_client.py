"""Antigravity (agy) CLI subprocess wrapper with stream parser.

Replaces the legacy Gemini CLI wrapper. `agy` is invoked in headless mode
via `agy chat -p` and streams text output which we parse into Loom events.

The `PreToolUse` hook is written to `.gemini/settings.json` (same format
as the legacy Gemini CLI) so tool permission requests route through
cc_permission_hook.py → Loom HTTP API → browser UI.

Session resume uses `--conversation <id>`.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Use the standard Loom blocking hook
_HOOK_SCRIPT = str(Path(__file__).parent / "cc_permission_hook.py")


def _find_agy_exe() -> str:
    """Find the `agy` executable. Checks PATH first, then known install locations."""
    # Check if agy is on PATH
    agy_name = "agy.exe" if sys.platform == "win32" else "agy"
    
    # Known Windows install location
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        known_path = os.path.join(local_app_data, "agy", "bin", "agy.exe")
        if os.path.exists(known_path):
            return known_path
        # Fallback: the older Antigravity install
        known_path2 = os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Programs", "Antigravity", "bin", "antigravity.cmd"
        )
        if os.path.exists(known_path2):
            return known_path2

    return agy_name  # Rely on PATH


def _process_event(raw: dict) -> list[dict]:
    """Process a raw stream-json event and return simplified event dicts.
    
    agy uses the same NDJSON stream format as Gemini CLI when invoked with
    --output-format stream-json.
    """
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
    """Configure PreToolUse / BeforeTool hook so agy routes tool approvals through Loom.
    
    Writes to .gemini/settings.json (agy reads this) and ensures the hook
    command is trusted.
    """
    gemini_dir = Path(cwd) / ".gemini"
    gemini_dir.mkdir(parents=True, exist_ok=True)

    python_exe = sys.executable
    hook_path = _HOOK_SCRIPT
    # agy runs hooks via PowerShell on Windows — use & (call operator)
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

    # Detect protocol (HTTPS if certs exist)
    _certs_dir = Path(__file__).parent / "certs"
    protocol = "https" if (_certs_dir / "cert.pem").exists() and (_certs_dir / "key.pem").exists() else "http"

    # Write project-level settings.json
    settings_path = gemini_dir / "settings.json"
    existing = {}
    if settings_path.exists():
        try: existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except: existing = {}

    # agy uses PreToolUse (like Claude Code), not BeforeTool (like legacy Gemini)
    existing["hooks"] = {"PreToolUse": hook_def}
    
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
    log.info(f"[AGY] Hook configured: {settings_path}")

    # Add hook command to trusted hooks
    _ensure_hook_trusted(cwd, hook_command)


def _ensure_hook_trusted(cwd: str, hook_command: str):
    """Add our hook command to the trusted_hooks.json for this project path."""
    if sys.platform == "win32":
        home = Path(os.environ.get("USERPROFILE", Path.home()))
    else:
        home = Path.home()
    trusted_path = home / ".gemini" / "trusted_hooks.json"

    trusted = {}
    if trusted_path.exists():
        try: trusted = json.loads(trusted_path.read_text(encoding="utf-8"))
        except: trusted = {}

    trust_key = f":{hook_command}"
    norm_cwd = str(Path(cwd).resolve())

    project_hooks = trusted.get(norm_cwd, [])
    if trust_key not in project_hooks:
        project_hooks.append(trust_key)
        trusted[norm_cwd] = project_hooks
        trusted_path.parent.mkdir(parents=True, exist_ok=True)
        trusted_path.write_text(json.dumps(trusted, indent=2), encoding="utf-8")
        log.info(f"[AGY] Hook trusted for {norm_cwd}")


async def run_gemini(prompt: str, cwd: str, conv_id: int = 0, server_port: int = 8000,
                     model: str = "sonnet", effort: str = "high",
                     permission_mode: str = "default",
                     resume_session_id: str = None, fork_session: bool = False,
                     backstage_parent_id: int | None = None):
    """Launch agy (Antigravity CLI) in headless mode and stream events.
    
    Returns (process, async_generator) matching the interface expected by
    server.py's generation loop.
    
    Function name kept as run_gemini for backwards compatibility with server.py
    imports.
    """
    _configure_permission_hook(cwd, backstage_parent_id, server_port)

    agy_exe = _find_agy_exe()

    # agy uses the 'chat' subcommand for headless prompting
    cc_args = [
        "chat",
        "--model", model,
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
    ]
    if resume_session_id:
        cc_args.extend(["--conversation", resume_session_id])

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

    env = {
        **os.environ,
        "LOOM_CONV_ID": str(conv_id),
        "LOOM_PORT": str(server_port),
    }
    if backstage_parent_id:
        env["LOOM_BACKSTAGE_PARENT_ID"] = str(backstage_parent_id)

    # Always pipe prompt via stdin on Windows — newlines in command-line args
    # get mangled by CreateProcess.
    use_stdin = sys.platform == "win32" or len(prompt) > 20000
    if use_stdin:
        cc_args.extend(["-p", "-"])  # read prompt from stdin
    else:
        cc_args.extend(["-p", prompt])

    cmd = [agy_exe] + cc_args

    print(f"[AGY] Launching: {' '.join(cmd[:6])}...")
    print(f"[AGY] Prompt length: {len(prompt)} chars (stdin={use_stdin})")
    print(f"[AGY] Prompt preview: {repr(prompt[:200])}")

    kwargs = {}
    if sys.platform == "win32":
        import subprocess
        # Use CREATE_NO_WINDOW and CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = 0x08000000 | 0x00000200

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdin=asyncio.subprocess.PIPE if use_stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
        **kwargs
    )

    # Feed prompt via stdin if needed
    if use_stdin and proc.stdin:
        async def _feed_stdin():
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            except Exception as e:
                log.error(f"[AGY] Error feeding stdin: {e}")
        asyncio.create_task(_feed_stdin())

    async def _read_stderr():
        async for line in proc.stderr:
            text = line.decode("utf-8", errors="replace").strip()
            if text: print(f"[AGY-stderr] {text}")
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
    """Cancel the agy subprocess. Name kept for backwards compat with server.py."""
    if proc.returncode is None:
        if sys.platform == 'win32':
            import subprocess
            try: subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True, timeout=5)
            except: proc.kill()
        else:
            proc.terminate()
        try: await asyncio.wait_for(proc.wait(), timeout=5)
        except: proc.kill()
