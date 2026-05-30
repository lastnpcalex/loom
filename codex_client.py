"""ChatGPT Codex CLI subprocess wrapper.

Runs codex in headless mode (exec) with --json flag and streams the events.
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


def _find_codex_exe() -> str:
    """Find the codex executable on PATH or in known install locations."""
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        # 1. Standard installer path:
        known = os.path.join(local_app_data, "Programs", "OpenAI", "Codex", "bin", "codex.exe")
        if os.path.exists(known):
            return known
        
        # 2. User profile fallback (current bin):
        home = os.environ.get("USERPROFILE", "")
        known_home = os.path.join(home, ".codex", "packages", "standalone", "current", "bin", "codex.exe")
        if os.path.exists(known_home):
            return known_home
            
        # 3. User profile fallback (current root):
        known_home_alt = os.path.join(home, ".codex", "packages", "standalone", "current", "codex.exe")
        if os.path.exists(known_home_alt):
            return known_home_alt

        # 4. Search PATH using shutil.which (check codex.cmd first for npm, then codex.exe)
        import shutil
        cmd_path = shutil.which("codex.cmd")
        if cmd_path:
            return cmd_path
        
        exe_path = shutil.which("codex.exe")
        if exe_path:
            return exe_path
            
        fallback_path = shutil.which("codex")
        if fallback_path:
            return fallback_path
            
        return "codex.cmd"
    return "codex"


def _loom_model_to_codex(model: str) -> str:
    """Map Loom's model selection to codex model identifiers."""
    ml = model.lower()
    if "gpt-5.5" in ml:
        return "gpt-5.5"
    if "gpt-5.4-mini" in ml:
        return "gpt-5.4-mini"
    if "gpt-5.4" in ml:
        return "gpt-5.4"
    if "gpt-5.3-codex" in ml:
        return "gpt-5.3-codex"
    if "gpt-4o" in ml:
        return "gpt-4o"
    # Default to gpt-5.5 for codex in 2026
    return "gpt-5.5"



def _configure_permission_hook(cwd: str):
    """Configure hooks.json so Codex CLI calls our permission hook."""
    codex_dir = Path(cwd) / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)

    python_exe = sys.executable
    hook_path = _HOOK_SCRIPT
    if sys.platform == "win32":
        pre_hook_command = f'& "{python_exe}" "{hook_path}" --event PreToolUse'
        post_hook_command = f'& "{python_exe}" "{hook_path}" --event PostToolUse'
    else:
        pre_hook_command = f'"{python_exe}" "{hook_path}" --event PreToolUse'
        post_hook_command = f'"{python_exe}" "{hook_path}" --event PostToolUse'

    hooks_def = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": ".*",
                    "hooks": [{
                        "type": "command",
                        "command": pre_hook_command,
                        "timeout": 900000,
                    }]
                }
            ],
            "PostToolUse": [
                {
                    "matcher": ".*",
                    "hooks": [{
                        "type": "command",
                        "command": post_hook_command,
                        "timeout": 90000,
                    }]
                }
            ]
        }
    }

    hooks_path = codex_dir / "hooks.json"
    hooks_path.write_text(json.dumps(hooks_def, indent=2), encoding="utf-8")
    log.info(f"[CODEX] Hooks configured: {hooks_path}")


async def run_codex(prompt: str, cwd: str, conv_id: int = 0, server_port: int = 8000,
                    model: str = "Codex (GPT-4o)", effort: str = "high",
                    permission_mode: str = "default",
                    resume_session_id: str = None, fork_session: bool = False,
                    backstage_parent_id: int | None = None):
    """Launch codex in headless mode, parse its JSONL stream, and yield events in real time."""
    _configure_permission_hook(cwd)

    codex_model = _loom_model_to_codex(model)
    codex_exe = _find_codex_exe()

    # Build the codex exec arguments
    # Use - to read prompt from stdin (handles very large prompts perfectly)
    cc_args = []
    if resume_session_id:
        cc_args.extend(["resume", resume_session_id, "-"])
    else:
        cc_args.extend(["-"])

    cc_args.extend(["--json", "--dangerously-bypass-hook-trust"])

    # Model and configuration overrides
    cc_args.extend(["-m", codex_model])

    # If it's a reasoning model (o3-mini/o1), configure the effort
    if codex_model in ("o3-mini", "o1") and effort:
        cc_args.extend(["-c", f"model_reasoning_effort={effort}"])

    cmd = [codex_exe, "exec"] + cc_args
    print(f"[CODEX] CMD: {' '.join(cmd[:6])}{'...' if len(cmd) > 6 else ''}")
    print(f"[CODEX] codex_model={codex_model}, prompt_len={len(prompt)}")

    env = {
        **os.environ,
        "LOOM_CONV_ID": str(conv_id),
        "LOOM_PORT": str(server_port),
    }
    if backstage_parent_id:
        env["LOOM_BACKSTAGE_PARENT_ID"] = str(backstage_parent_id)

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000 | 0x00000200

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
        **kwargs
    )

    # Feed the prompt to stdin in the background
    async def _feed_stdin():
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception as e:
            log.error(f"[CODEX] Error feeding stdin: {e}")
    asyncio.create_task(_feed_stdin())

    # We read stderr in the background
    async def _read_stderr():
        try:
            async for line in proc.stderr:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    print(f"[CODEX-stderr] {text}")
        except Exception as e:
            log.error(f"[CODEX] Error reading stderr: {e}")
    asyncio.create_task(_read_stderr())

    async def _event_stream():
        session_id = resume_session_id or str(conv_id)
        full_text = ""
        got_result = False

        try:
            async for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    print(f"[CODEX] Non-JSON line on stdout: {line[:200]}")
                    continue

                etype = raw.get("type")
                if etype == "thread.started":
                    session_id = raw.get("thread_id", session_id)
                    yield {
                        "type": "session_info",
                        "session_id": session_id,
                        "model": codex_model,
                    }

                elif etype == "item.started":
                    item = raw.get("item") or {}
                    item_type = item.get("type")
                    item_id = item.get("id") or str(raw.get("timestamp", ""))
                    if item_type in ("mcp_tool_call", "command_execution"):
                        yield {
                            "type": "tool_start",
                            "name": item.get("name", item_type),
                            "tool_id": item_id,
                        }

                elif etype == "item.completed":
                    item = raw.get("item") or {}
                    item_type = item.get("type")
                    item_id = item.get("id") or str(raw.get("timestamp", ""))
                    content = item.get("text", "")

                    if item_type == "reasoning" and content:
                        chunk_size = 128
                        for i in range(0, len(content), chunk_size):
                            yield {"type": "thinking_delta", "text": content[i:i+chunk_size]}
                            await asyncio.sleep(0.005)

                    elif item_type == "agent_message" and content:
                        chunk_size = 64
                        for i in range(0, len(content), chunk_size):
                            yield {"type": "text_delta", "text": content[i:i+chunk_size]}
                            await asyncio.sleep(0.01)
                        full_text += content

                    elif item_type in ("mcp_tool_call", "command_execution"):
                        yield {
                            "type": "tool_result",
                            "content": item.get("output", ""),
                            "tool_id": item_id,
                            "is_error": item.get("status") == "failed",
                        }

                elif etype == "turn.completed":
                    got_result = True
                    yield {
                        "type": "result",
                        "is_error": False,
                        "result_text": full_text,
                        "session_id": session_id,
                    }
                    break

                elif etype in ("turn.failed", "error"):
                    got_result = True
                    err_msg = raw.get("error", {}).get("message", raw.get("message", "Unknown error"))
                    yield {
                        "type": "result",
                        "is_error": True,
                        "result_text": full_text,
                        "session_id": session_id,
                        "error": err_msg,
                    }
                    break

        except Exception as e:
            log.error(f"[CODEX] Error processing event stream: {e}")
            yield {
                "type": "result",
                "is_error": True,
                "result_text": full_text,
                "session_id": session_id,
                "error": str(e),
            }

        await proc.wait()

        # Emit default result if not already yielded
        if not got_result:
            yield {
                "type": "result",
                "is_error": proc.returncode != 0,
                "result_text": full_text,
                "session_id": session_id,
            }

    return proc, _event_stream()


async def cancel_codex(proc):
    """Kill a running Codex subprocess and its process tree."""
    if proc.returncode is None:
        if sys.platform == 'win32':
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
