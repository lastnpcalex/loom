"""Antigravity (agy) CLI subprocess wrapper.

Runs agy in plain text mode (-p flag) and collects the raw output.
Model is set via ~/.gemini/antigravity-cli/settings.json (not a CLI flag).
"""

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Use the standard Loom blocking hook
_HOOK_SCRIPT = str(Path(__file__).parent / "cc_permission_hook.py")

_active_queues: dict[int, asyncio.Queue] = {}


def _find_agy_exe() -> str:
    """Find the agy executable on PATH or in known install locations."""
    agy_name = "agy.exe" if sys.platform == "win32" else "agy"
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        known = os.path.join(local_app_data, "agy", "bin", "agy.exe")
        if os.path.exists(known):
            return known
    return agy_name


def _loom_model_to_agy(model: str, effort: str) -> str:
    """Map Loom's model selection to agy 2.0 model identifiers."""
    ml = model.lower()
    if "gemini 3.5 flash" in ml:
        return {
            "low": "gemini-3.5-flash-low",
            "medium": "gemini-3.5-flash-medium",
        }.get(effort, "gemini-3.5-flash")
    if "gemini 3.1 pro" in ml:
        return {
            "low": "gemini-3.1-pro-low",
            "medium": "gemini-3.1-pro-medium",
        }.get(effort, "gemini-3.1-pro")
    if "sonnet" in ml:
        return "claude-sonnet"
    if "opus" in ml:
        return "claude-opus"
    if "gpt-oss" in ml:
        return "gpt-oss-120b"
    return model


def _set_agy_model(agy_model_id: str):
    """Update agy's settings.json with the desired model."""
    if sys.platform == "win32":
        home = Path(os.environ.get("USERPROFILE", Path.home()))
    else:
        home = Path.home()
    settings_path = home / ".gemini" / "antigravity-cli" / "settings.json"
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            settings["model"] = agy_model_id
            settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            log.info(f"[AGY] Model updated in settings.json: {agy_model_id}")
        except Exception as e:
            log.error(f"[AGY] Failed to write model to settings.json: {e}")


def _configure_permission_hook(cwd: str, backstage_parent_id: int | None = None, server_port: int = 8000):
    """Configure hooks.json + trust entry so Antigravity CLI calls our permission hook."""
    agents_dir = Path(cwd) / ".agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    python_exe = sys.executable
    hook_path = _HOOK_SCRIPT
    if sys.platform == "win32":
        pre_hook_command = f'& "{python_exe}" "{hook_path}" --event PreToolUse'
        post_hook_command = f'& "{python_exe}" "{hook_path}" --event PostToolUse'
    else:
        pre_hook_command = f'"{python_exe}" "{hook_path}" --event PreToolUse'
        post_hook_command = f'"{python_exe}" "{hook_path}" --event PostToolUse'

    hooks_def = {
        "PreToolUse": [
            {
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": pre_hook_command,
                    "timeout": 900000,
                }]
            }
        ],
        "PostToolUse": [
            {
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": post_hook_command,
                    "timeout": 90000,
                }]
            }
        ]
    }

    hooks_path = agents_dir / "hooks.json"
    hooks_path.write_text(json.dumps(hooks_def, indent=2), encoding="utf-8")
    log.info(f"[AGY] Hooks configured: {hooks_path}")

    if backstage_parent_id:
        _certs_dir = Path(__file__).parent / "certs"
        protocol = "https" if (_certs_dir / "cert.pem").exists() and (_certs_dir / "key.pem").exists() else "http"
        mcp_config = {
            "mcpServers": {
                "loom-state-cards": {
                    "command": python_exe,
                    "args": [str(Path(__file__).parent / "mcp_state_cards.py")],
                    "env": {
                        "LOOM_API_URL": f"{protocol}://127.0.0.1:{server_port}",
                        "LOOM_BACKSTAGE_PARENT_ID": str(backstage_parent_id)
                    }
                }
            }
        }
        mcp_path = agents_dir / "mcp_config.json"
        mcp_path.write_text(json.dumps(mcp_config, indent=2), encoding="utf-8")
        log.info(f"[AGY] MCP server configured: {mcp_path}")

    _ensure_hook_trusted(cwd, pre_hook_command)
    _ensure_hook_trusted(cwd, post_hook_command)


def _ensure_hook_trusted(cwd: str, hook_command: str):
    """Add our hook command to agy's trusted_hooks.json for this project path."""
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
                     model: str = "Gemini 3.5 Flash (High)", effort: str = "high",
                     permission_mode: str = "default",
                     resume_session_id: str = None, fork_session: bool = False,
                     backstage_parent_id: int | None = None):
    """Launch agy in headless mode and yield events.

    Uses plain -p mode (no --output-format). agy runs to completion and outputs
    raw text to stdout. We collect it all, strip ANSI codes, and forward as
    text_delta events followed by a result event.
    """
    _configure_permission_hook(cwd, backstage_parent_id, server_port)

    agy_model = _loom_model_to_agy(model, effort)
    _set_agy_model(agy_model)

    queue = asyncio.Queue()
    _active_queues[conv_id] = queue

    # agy 1.0.2 does not support --output-format. Run in plain -p mode
    # and collect the raw text output.
    agy_exe = _find_agy_exe()
    cc_args = [
        "--conversation", resume_session_id or str(conv_id),
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--print-timeout", "5m",
    ]

    env = {
        **os.environ,
        "LOOM_CONV_ID": str(conv_id),
        "LOOM_PORT": str(server_port),
    }
    if backstage_parent_id:
        env["LOOM_BACKSTAGE_PARENT_ID"] = str(backstage_parent_id)

    cmd = [agy_exe] + cc_args
    print(f"[AGY] CMD: {' '.join(cmd[:6])}{'...' if len(cmd) > 6 else ''}")
    print(f"[AGY] agy_model={agy_model}, prompt_len={len(prompt)}")

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000 | 0x00000200

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
        **kwargs
    )

    # Collect stdout, strip ANSI, and look for a JSON block at the end.
    async def _read_stdout_to_queue(stdout, queue):
        buf = b""
        try:
            while True:
                chunk = await stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
        except Exception as e:
            log.error(f"[AGY] Error reading stdout: {e}")
        finally:
            raw_text = buf.decode("utf-8", errors="replace")
            # Strip ANSI escape codes
            raw_text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', raw_text)
            raw_text = raw_text.strip()
            if raw_text:
                try:
                    parsed = json.loads(raw_text)
                    # {"response": "...", "stats": {...}, "error": null}
                    response_text = parsed.get("response", "") or ""
                    stats = parsed.get("stats", {})
                    err = parsed.get("error")
                    if response_text:
                        queue.put_nowait({"type": "text_delta", "text": response_text})
                    if stats:
                        queue.put_nowait({
                            "type": "usage",
                            "input_tokens": stats.get("inputTokens", stats.get("input_tokens", 0)),
                            "output_tokens": stats.get("outputTokens", stats.get("output_tokens", 0)),
                        })
                        queue.put_nowait({
                            "type": "result",
                            "duration_ms": stats.get("latencyMs", stats.get("duration_ms", 0)),
                            "result_text": response_text,
                            "is_error": err is not None,
                        })
                    elif err:
                        queue.put_nowait({
                            "type": "result",
                            "is_error": True,
                            "error": str(err),
                        })
                    else:
                        # Empty response — no error, no text
                        queue.put_nowait({"type": "result", "is_error": False})
                except json.JSONDecodeError:
                    # Not JSON — treat as plain text
                    lines = [l for l in raw_text.split("\n") if l.strip()]
                    for line in lines:
                        queue.put_nowait({"type": "text_delta", "text": line + "\n"})
                    queue.put_nowait({"type": "result", "is_error": False})
            queue.put_nowait(None)

    asyncio.create_task(_read_stdout_to_queue(proc.stdout, queue))

    async def _read_stderr():
        async for line in proc.stderr:
            text = line.decode("utf-8", errors="replace").strip()
            if text: print(f"[AGY-stderr] {text}")
    asyncio.create_task(_read_stderr())

    async def _event_stream():
        yield {
            "type": "session_info",
            "session_id": resume_session_id or str(conv_id),
            "model": agy_model,
        }

        stdout_done = False
        got_result = False
        while not (stdout_done and queue.empty()):
            try:
                evt = await queue.get()
                if evt is None:
                    stdout_done = True
                    continue
                if evt.get("type") == "result":
                    got_result = True
                yield evt
            except asyncio.CancelledError:
                break

        _active_queues.pop(conv_id, None)
        await proc.wait()

        # Only emit a synthetic result if the JSON block didn't produce one.
        if not got_result:
            yield {
                "type": "result",
                "is_error": proc.returncode != 0,
                "result_text": "",
                "session_id": resume_session_id or str(conv_id),
            }

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
