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


def normalize_tool_name(name: str) -> str:
    mapping = {
        "view_file": "Read",
        "read_file": "Read",
        "read_many_files": "Read",
        "write_to_file": "Write",
        "write_file": "Write",
        "replace_file_content": "Edit",
        "multi_replace_file_content": "Edit",
        "run_command": "Bash",
        "execute_command": "Bash",
        "run_shell_command": "Bash",
        "grep_search": "Grep",
        "list_dir": "Glob",
        "list_directory": "Glob",
        "glob": "Glob",
        "search_web": "WebSearch",
        "google_web_search": "WebSearch",
        "read_url_content": "WebFetch",
        "web_fetch": "WebFetch",
    }
    return mapping.get(name, name)


def clean_arg_val(val):
    if isinstance(val, str):
        if val.startswith('"') and val.endswith('"') and len(val) >= 2:
            return val[1:-1]
    return val


def normalize_tool_args(name: str, args: dict) -> dict:
    cleaned = {k: clean_arg_val(v) for k, v in args.items()}
    mapped = {}
    if name in ("view_file", "read_file", "read_many_files"):
        mapped["file_path"] = cleaned.get("AbsolutePath", cleaned.get("TargetFile", cleaned.get("file_path", "")))
    elif name in ("write_to_file", "write_file"):
        mapped["file_path"] = cleaned.get("TargetFile", cleaned.get("file_path", ""))
        mapped["content"] = cleaned.get("CodeContent", cleaned.get("content", ""))
    elif name in ("replace_file_content", "multi_replace_file_content", "edit"):
        mapped["file_path"] = cleaned.get("TargetFile", cleaned.get("file_path", ""))
        mapped["old_string"] = cleaned.get("TargetContent", cleaned.get("old_string", ""))
        mapped["new_string"] = cleaned.get("ReplacementContent", cleaned.get("new_string", ""))
    elif name in ("run_command", "execute_command", "run_shell_command"):
        mapped["command"] = cleaned.get("CommandLine", cleaned.get("command", ""))
    elif name in ("grep_search", "grep"):
        mapped["query"] = cleaned.get("Query", cleaned.get("query", ""))
        mapped["dir"] = cleaned.get("SearchPath", cleaned.get("dir", ""))
    else:
        mapped = cleaned
    return mapped


async def run_gemini(prompt: str, cwd: str, conv_id: int = 0, server_port: int = 8000,
                     model: str = "Gemini 3.5 Flash (High)", effort: str = "high",
                     permission_mode: str = "default",
                     resume_session_id: str = None, fork_session: bool = False,
                     backstage_parent_id: int | None = None):
    """Launch agy in headless mode, watch its transcript, and yield events in real time."""
    _configure_permission_hook(cwd, backstage_parent_id, server_port)

    agy_model = _loom_model_to_agy(model, effort)
    _set_agy_model(agy_model)

    queue = asyncio.Queue()
    _active_queues[conv_id] = queue

    # Setup transcript watching paths
    if sys.platform == "win32":
        home = Path(os.environ.get("USERPROFILE", Path.home()))
    else:
        home = Path.home()
    brain_path = home / ".gemini" / "antigravity-cli" / "brain"

    def find_latest_transcript(path: Path) -> tuple[Path | None, float]:
        latest_file = None
        latest_time = 0.0
        if path.exists():
            for root, dirs, files in os.walk(path):
                for f in files:
                    if f == "transcript.jsonl":
                        fp = Path(root) / f
                        try:
                            mtime = fp.stat().st_mtime
                            if mtime > latest_time:
                                latest_time = mtime
                                latest_file = fp
                        except OSError:
                            pass
        return latest_file, latest_time

    # Determine baseline before launching process to avoid race conditions
    use_resume = bool(resume_session_id)
    baseline_file, baseline_time = find_latest_transcript(brain_path)
    initial_size = 0
    if use_resume and baseline_file:
        try:
            initial_size = baseline_file.stat().st_size
        except OSError:
            pass

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

    # We read stderr in the background
    async def _read_stderr():
        try:
            async for line in proc.stderr:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    print(f"[AGY-stderr] {text}")
        except Exception as e:
            log.error(f"[AGY] Error reading stderr: {e}")
    asyncio.create_task(_read_stderr())

    # We also drain stdout to avoid blocking
    async def _drain_stdout():
        try:
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
        except Exception as e:
            log.error(f"[AGY] Error draining stdout: {e}")
    asyncio.create_task(_drain_stdout())

    # Asynchronous task to monitor and tail the transcript.jsonl file
    async def _tail_transcript_to_queue():
        active_file = None
        # Poll for the active transcript file being created or modified (10s timeout)
        for _ in range(200):
            latest_file, latest_time = find_latest_transcript(brain_path)
            if latest_file and (latest_file != baseline_file or latest_time > baseline_time + 0.1):
                active_file = latest_file
                break
            await asyncio.sleep(0.05)

        if not active_file:
            log.error("[AGY] Timeout waiting for active transcript file. Waiting for process completion.")
            await proc.wait()
            queue.put_nowait({
                "type": "result",
                "is_error": proc.returncode != 0,
                "result_text": "",
                "session_id": resume_session_id or str(conv_id),
            })
            queue.put_nowait(None)
            return

        session_id = ""
        try:
            rel = active_file.relative_to(brain_path)
            session_id = rel.parts[0]
        except ValueError:
            session_id = active_file.parent.parent.parent.parent.name

        # Put session info event into queue
        queue.put_nowait({
            "type": "session_info",
            "session_id": session_id,
            "model": agy_model,
        })

        # Wait a moment for file updates to begin
        await asyncio.sleep(0.1)
        
        try:
            with open(active_file, "r", encoding="utf-8", errors="replace") as f:
                if active_file == baseline_file and initial_size > 0:
                    f.seek(initial_size)
                    f.readline()  # consume any trailing fragment of the previous line
                
                full_text = ""
                processed_steps = set()
                active_tool_calls = []

                while True:
                    line = f.readline()
                    if line:
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                            step_index = event.get("step_index")
                            if step_index in processed_steps:
                                continue
                            processed_steps.add(step_index)

                            etype = event.get("type")
                            source = event.get("source")
                            content = event.get("content")
                            tool_calls = event.get("tool_calls")

                            if etype == "PLANNER_RESPONSE":
                                thinking = event.get("thinking")
                                content = event.get("content")

                                # Stream thinking process if present
                                if thinking:
                                    chunk_size = 128
                                    for i in range(0, len(thinking), chunk_size):
                                        chunk = thinking[i:i+chunk_size]
                                        queue.put_nowait({"type": "thinking_delta", "text": chunk})
                                        await asyncio.sleep(0.005)

                                # Process tool calls with Claude-compatible mappings
                                if tool_calls:
                                    for idx, tool in enumerate(tool_calls):
                                        tool_id = str(step_index)
                                        tool_name = tool.get("name")
                                        tool_args = tool.get("args", {})
                                        
                                        mapped_name = normalize_tool_name(tool_name)
                                        mapped_args = normalize_tool_args(tool_name, tool_args)
                                        
                                        active_tool_calls.append(tool_id)
                                        
                                        queue.put_nowait({
                                            "type": "tool_start",
                                            "name": mapped_name,
                                            "tool_id": tool_id,
                                        })
                                        queue.put_nowait({
                                            "type": "tool_input_delta",
                                            "json": json.dumps(mapped_args, indent=2),
                                            "tool_id": tool_id,
                                        })
                                        if tool_name in ("ask_question", "AskUserQuestion"):
                                            queue.put_nowait({
                                                "type": "ask_user_question",
                                                "questions": tool_args.get("questions", []),
                                                "tool_id": tool_id,
                                            })
                                        elif tool_name in ("ExitPlanMode", "exit_plan_mode"):
                                            queue.put_nowait({
                                                "type": "plan_ready",
                                                "plan": tool_args.get("plan", ""),
                                                "plan_file": tool_args.get("planFilePath", tool_args.get("plan_file", "")),
                                                "tool_id": tool_id,
                                            })

                                # Stream content (commentary or final response) as text_delta
                                if content:
                                    chunk_size = 64
                                    for i in range(0, len(content), chunk_size):
                                        chunk = content[i:i+chunk_size]
                                        queue.put_nowait({"type": "text_delta", "text": chunk})
                                        await asyncio.sleep(0.01)
                                    full_text += content

                            elif etype not in ("USER_INPUT", "CONVERSATION_HISTORY"):
                                if active_tool_calls:
                                    tool_id = active_tool_calls.pop(0)
                                    queue.put_nowait({
                                        "type": "tool_result",
                                        "content": content or "",
                                        "tool_id": tool_id,
                                        "is_error": False,
                                    })
                                else:
                                    if content:
                                        queue.put_nowait({
                                            "type": "text_delta",
                                            "text": f"\n[Tool Output]: {content}\n",
                                        })
                        except Exception as e:
                            log.error(f"[AGY] Error parsing transcript line: {e}")
                    else:
                        # EOF. Check if process is finished
                        if proc.returncode is not None:
                            # Read any remaining lines
                            for line in f:
                                if not line.strip():
                                    continue
                                try:
                                    event = json.loads(line)
                                    step_index = event.get("step_index")
                                    if step_index in processed_steps:
                                        continue
                                    processed_steps.add(step_index)

                                    etype = event.get("type")
                                    content = event.get("content")
                                    thinking = event.get("thinking")
                                    tool_calls = event.get("tool_calls")

                                    if etype == "PLANNER_RESPONSE":
                                        if thinking:
                                            queue.put_nowait({"type": "thinking_delta", "text": thinking})

                                        if tool_calls:
                                            for idx, tool in enumerate(tool_calls):
                                                tool_id = str(step_index)
                                                tool_name = tool.get("name")
                                                tool_args = tool.get("args", {})
                                                
                                                mapped_name = normalize_tool_name(tool_name)
                                                mapped_args = normalize_tool_args(tool_name, tool_args)
                                                
                                                active_tool_calls.append(tool_id)
                                                queue.put_nowait({
                                                    "type": "tool_start",
                                                    "name": mapped_name,
                                                    "tool_id": tool_id,
                                                })
                                                queue.put_nowait({
                                                    "type": "tool_input_delta",
                                                    "json": json.dumps(mapped_args, indent=2),
                                                    "tool_id": tool_id,
                                                })
                                                if tool_name in ("ask_question", "AskUserQuestion"):
                                                    queue.put_nowait({
                                                        "type": "ask_user_question",
                                                        "questions": tool_args.get("questions", []),
                                                        "tool_id": tool_id,
                                                    })
                                                elif tool_name in ("ExitPlanMode", "exit_plan_mode"):
                                                    queue.put_nowait({
                                                        "type": "plan_ready",
                                                        "plan": tool_args.get("plan", ""),
                                                        "plan_file": tool_args.get("planFilePath", tool_args.get("plan_file", "")),
                                                        "tool_id": tool_id,
                                                    })
                                        if content:
                                            queue.put_nowait({"type": "text_delta", "text": content})
                                            full_text += content
                                    elif etype not in ("USER_INPUT", "CONVERSATION_HISTORY"):
                                        if active_tool_calls:
                                            tool_id = active_tool_calls.pop(0)
                                            queue.put_nowait({
                                                "type": "tool_result",
                                                "content": content or "",
                                                "tool_id": tool_id,
                                                "is_error": False,
                                            })
                                        else:
                                            if content:
                                                queue.put_nowait({
                                                    "type": "text_delta",
                                                    "text": f"\n[Tool Output]: {content}\n",
                                                })
                                except Exception as e:
                                    log.error(f"[AGY] Error parsing final transcript line: {e}")
                            break
                        await asyncio.sleep(0.05)

                queue.put_nowait({
                    "type": "result",
                    "is_error": proc.returncode != 0,
                    "result_text": full_text,
                    "session_id": session_id,
                })
        except Exception as e:
            log.error(f"[AGY] Error tailing transcript: {e}")
            queue.put_nowait({
                "type": "result",
                "is_error": True,
                "error": str(e),
                "session_id": session_id,
            })
        finally:
            queue.put_nowait(None)

    asyncio.create_task(_tail_transcript_to_queue())

    async def _event_stream():
        got_result = False
        queue_done = False
        
        yielded_starts = set()
        yielded_results = set()
        yielded_questions = set()
        yielded_plans = set()
        
        while not (queue_done and queue.empty()):
            try:
                evt = await queue.get()
                if evt is None:
                    queue_done = True
                    continue
                
                etype = evt.get("type")
                tool_id = evt.get("tool_id")
                
                if etype == "tool_start":
                    if tool_id in yielded_starts:
                        continue
                    yielded_starts.add(tool_id)
                    
                elif etype == "tool_result":
                    if tool_id in yielded_results:
                        continue
                    yielded_results.add(tool_id)
                    
                elif etype == "ask_user_question":
                    if tool_id in yielded_questions:
                        continue
                    yielded_questions.add(tool_id)
                    
                elif etype == "plan_ready":
                    if tool_id in yielded_plans:
                        continue
                    yielded_plans.add(tool_id)
                    
                if evt.get("type") == "result":
                    got_result = True
                yield evt
            except asyncio.CancelledError:
                break

        _active_queues.pop(conv_id, None)
        await proc.wait()

        # Emit default result if not already yielded
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
