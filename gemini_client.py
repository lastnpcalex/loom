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

from loom_agent_prompt import prepend_loom_agent_context

log = logging.getLogger(__name__)

# Use the standard Loom blocking hook
_HOOK_SCRIPT = str(Path(__file__).parent / "cc_permission_hook.py")

_active_queues: dict[int, asyncio.Queue] = {}


def _agy_home() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("USERPROFILE", Path.home())) / ".gemini" / "antigravity-cli"
    return Path.home() / ".gemini" / "antigravity-cli"


_RE_RESETS_IN = re.compile(r"Resets in ([0-9hms]+)")


def _parse_agy_log_filename_ts(filename: str) -> float | None:
    import time as _time
    import re
    m = re.match(r"cli-(\d{8})_(\d{6})\.log", filename)
    if not m:
        return None
    date_str, time_str = m.groups()
    try:
        dt_str = f"{date_str} {time_str}"
        struct_time = _time.strptime(dt_str, "%Y%m%d %H%M%S")
        return _time.mktime(struct_time)
    except Exception:
        return None


def _scan_agy_log_for_error(since_ts: float) -> str | None:
    """Look at the newest agy cli-*.log for a fatal signal (429/auth) since `since_ts`.

    agy writes its real error to its own log file rather than stderr, so when
    the transcript never materializes we have to read this to surface a useful
    message instead of "exited with no response".
    """
    log_dir = _agy_home() / "log"
    if not log_dir.exists():
        return None
    try:
        candidates = []
        for p in log_dir.glob("cli-*.log"):
            filename_ts = _parse_agy_log_filename_ts(p.name)
            # Filter log files created within 30s of launch_ts to protect against OneDrive sync time skew
            if filename_ts is not None and abs(filename_ts - since_ts) <= 30.0:
                candidates.append(p)
    except OSError:
        return None
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    if "RESOURCE_EXHAUSTED" in text or "Individual quota reached" in text:
        m = _RE_RESETS_IN.search(text)
        resets = f" — resets in {m.group(1)}" if m else ""
        return f"Antigravity quota reached (429){resets}"
    if "You are not logged into Antigravity" in text:
        success_indicators = [
            "silent auth succeeded",
            "authenticated successfully",
            "Auth succeeded",
        ]
        if not any(indicator in text for indicator in success_indicators):
            return "Antigravity is not logged in — run `agy` interactively to sign in"
    if "PERMISSION_DENIED" in text:
        return "Antigravity permission denied — check account access"
    return None


def _is_agy_alive(launch_ts: float) -> bool:
    """Check if agy is alive by watching its CLI log mtime.

    agy writes to its log for every API call (streamGenerateContent, etc).
    A recent mtime means agy is actively processing, even if the transcript
    hasn't flushed yet.
    """
    import time as _time

    log_dir = _agy_home() / "log"
    if not log_dir.exists():
        return False
    try:
        candidates = [p for p in log_dir.glob("cli-*.log")
                       if _parse_agy_log_filename_ts(p.name) is not None
                       and abs(_parse_agy_log_filename_ts(p.name) - launch_ts) <= 30.0]
        if not candidates:
            return False
        latest = max(candidates, key=lambda p: p.stat().st_mtime)
        mtime = latest.stat().st_mtime
        return (_time.time() - mtime) < 45.0  # alive if log touched within last 45s
    except (OSError, ValueError):
        return False


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
    if model.lower().startswith("gemini:"):
        model = model[7:]
    ml = model.lower()
    if "gemini 3.5 flash" in ml or "gemini-3.5-flash" in ml:
        return {
            "low": "gemini-3.5-flash-low",
            "medium": "gemini-3.5-flash-medium",
            "high": "gemini-3.5-flash-medium",
        }.get(effort, "gemini-3.5-flash-medium")
    if "gemini 3.1 pro" in ml or "gemini-3.1-pro" in ml:
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

    # PreToolUse: 24 hours — longer than Loom server-side _PERM_TOTAL_DEADLINE
    # so the server can send its deny/allow before the hook times out.
    # User can disconnect, come back, reattach the WS, and approve the hook.
    hooks_def = {
        "PreToolUse": {
            "matcher": "*",
            "hooks": [{
                "type": "command",
                "command": pre_hook_command,
                "timeout": 86400000,
            }]
        },
        "PostToolUse": {
            "matcher": "*",
            "hooks": [{
                "type": "command",
                "command": post_hook_command,
                "timeout": 90000,
            }]
        }
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
    else:
        # Neutral agy launch (no backstage, no operator). agy reads MCP config
        # from .agents/ at the git-root workspace it discovers — deleting that
        # file breaks agy's project discovery (falls back to default-cli-project
        # with no MCP tools). Instead of deleting, write an empty config so the
        # directory stays discoverable but registers no servers.
        agy_root = _agy_workspace_root(Path(cwd))
        for target in (Path(cwd), agy_root):
            stale = target / ".agents" / "mcp_config.json"
            if stale.exists():
                stale.write_text(json.dumps({"mcpServers": {}}, indent=2), encoding="utf-8")
                log.info(f"[AGY] Cleared stale mcp_config.json: {stale}")

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


def _agy_workspace_root(cwd: Path) -> Path:
    """Where agy actually anchors its workspace.

    agy walks up from cwd to find a project root marker (`.git` in practice)
    and uses that as the singular entry in `workspaceDirs`. Its `.agents/`
    and `GEMINI.md` discovery happens at that root, not at cwd. So when the
    operator workspace is a subdirectory of a larger git repo (the Loom repo,
    in our case), files dropped in `cwd/.agents/` are invisible — agy reads
    the repo-root `.agents/` instead. This walks the same path agy does so
    we can write where it will actually look.
    """
    current = cwd.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def _get_agy_project_id(workspace: Path) -> str | None:
    """Read the workspace's project ID from agy's project cache.

    agy stores workspace→project mappings in
    `~/.gemini/antigravity-cli/cache/projects.json`. Without the correct
    project ID, new conversations fall into the default-cli-project which
    has no MCP servers configured.
    """
    sys_home = Path(os.environ.get("USERPROFILE", Path.home())) if sys.platform == "win32" else Path.home()
    cache_file = sys_home / ".gemini" / "antigravity-cli" / "cache" / "projects.json"
    if not cache_file.exists():
        return None
    try:
        projects = json.loads(cache_file.read_text(encoding="utf-8"))
        ws_key = str(workspace.resolve())
        return projects.get(ws_key)
    except (json.JSONDecodeError, OSError):
        return None


def _configure_operator(cwd: str, conv_id: int, server_port: int):
    """NROL operator lockdown for agy: role instructions + strict MCP surface.

    Tool blocking itself is the permission hook's NROL deny-list, keyed on
    LOOM_NROL_OPERATOR (set in run_gemini): the installed agy CLI exposes no
    excludeTools/coreTools settings surface (verified 2026-06-11), so unlike
    claude there is no true tool removal — write/shell attempts are denied by
    the PreToolUse hook without a prompt. See ROADMAP.md "Multi-provider
    operator parity".
    """
    workspace = Path(cwd)
    agy_root = _agy_workspace_root(workspace)

    # agy auto-loads GEMINI.md and `.agents/mcp_config.json` from the
    # workspace root it discovers (walking up to `.git`), not from cwd. Write
    # to BOTH so we don't depend on whether the operator workspace happens to
    # be the repo root or a subdirectory under it.
    operator_md = Path(__file__).parent / "mcp_servers" / "nrol_ao" / "OPERATOR.md"
    if operator_md.is_file():
        operator_md_text = operator_md.read_text(encoding="utf-8")
        (workspace / "GEMINI.md").write_text(operator_md_text, encoding="utf-8")
        if agy_root != workspace:
            (agy_root / "GEMINI.md").write_text(operator_md_text, encoding="utf-8")

    # Strict MCP surface: exactly nrol-ao + web-tools. Reuse codex_client's
    # builders, stripped to the keys agy's mcp_config.json understands.
    from codex_client import _nrol_mcp_config, _web_tools_mcp_config

    mcp_servers = {}
    nrol_cfg = _nrol_mcp_config(conv_id, server_port, force=True)
    if nrol_cfg:
        mcp_servers["nrol-ao"] = {k: nrol_cfg[k] for k in ("command", "args", "env")}
    web_cfg = _web_tools_mcp_config()
    if web_cfg:
        mcp_servers["web-tools"] = {k: web_cfg[k] for k in ("command", "args")}

    mcp_json = json.dumps({"mcpServers": mcp_servers}, indent=2)
    for target in {workspace, agy_root}:
        agents_dir = target / ".agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "mcp_config.json").write_text(mcp_json, encoding="utf-8")
        log.info(f"[AGY] NROL operator MCP surface configured: {agents_dir / 'mcp_config.json'}")


def _prepare_agy_prompt(
    prompt: str,
    backstage_parent_id: int | None = None,
    nrol_operator: bool = False,
) -> str:
    """Inject the Loom contract for ordinary agy sessions."""
    if backstage_parent_id or nrol_operator:
        return prompt
    return prepend_loom_agent_context(prompt, "agy")


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


def _determine_block_name(etype: str, content: str) -> str:
    et = etype.upper()
    if "SYSTEM" in et:
        return "System Message"
    if "GENERIC" in et:
        if "background task" in content.lower():
            return "Background Task"
        return "System Message"
    return etype.replace("_", " ").title()


async def run_gemini(prompt: str, cwd: str, conv_id: int = 0, server_port: int = 8000,
                     model: str = "Gemini 3.5 Flash (High)", effort: str = "high",
                     permission_mode: str = "default",
                     resume_session_id: str = None, fork_session: bool = False,
                     backstage_parent_id: int | None = None,
                     nrol_operator: bool = False):
    if permission_mode == "plan":
        plan_instruction = (
            "You are running in PLAN MODE. Your task is to analyze the codebase and write a comprehensive "
            "implementation plan to `implementation_plan.md` in the workspace. Do NOT modify any other files "
            "or run commands that modify the repository. Once the plan is written, present it to the user "
            "and ask for their approval. After writing the plan, end your turn immediately without performing "
            "any edits."
        )
        prompt = f"{plan_instruction}\n\n{prompt}"

    _configure_permission_hook(cwd, backstage_parent_id, server_port)
    if nrol_operator:
        _configure_operator(cwd, conv_id, server_port)
    else:
        prompt = _prepare_agy_prompt(prompt, backstage_parent_id, nrol_operator)

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

    # Fork session if requested
    if resume_session_id and fork_session:
        import uuid
        new_session_id = str(uuid.uuid4())
        src = brain_path / resume_session_id
        dst = brain_path / new_session_id
        if src.exists() and not dst.exists():
            import shutil
            try:
                shutil.copytree(src, dst)
                print(f"[AGY] Forked session {resume_session_id} to new session {new_session_id}")
                
                # Also fork the conversation database/pb in the conversations folder
                conversations_path = brain_path.parent / "conversations"
                if conversations_path.exists():
                    db_src = conversations_path / f"{resume_session_id}.db"
                    db_dst = conversations_path / f"{new_session_id}.db"
                    if db_src.exists():
                        shutil.copy2(db_src, db_dst)
                        # Also copy WAL and SHM if they exist
                        for suffix in [".db-wal", ".db-shm"]:
                            sub_src = conversations_path / f"{resume_session_id}{suffix}"
                            sub_dst = conversations_path / f"{new_session_id}{suffix}"
                            if sub_src.exists():
                                try:
                                    shutil.copy2(sub_src, sub_dst)
                                except Exception as se:
                                    log.warning(f"[AGY] Failed to copy {suffix} file: {se}")
                        # Update cascade_id in trajectory_meta table
                        import sqlite3
                        try:
                            conn = sqlite3.connect(db_dst)
                            cursor = conn.cursor()
                            cursor.execute(
                                "UPDATE trajectory_meta SET cascade_id = ? WHERE cascade_id = ?",
                                (new_session_id, resume_session_id)
                            )
                            conn.commit()
                            conn.close()
                            print(f"[AGY] Updated cascade_id in {db_dst.name}")
                        except Exception as sqle:
                            log.warning(f"[AGY] Failed to update sqlite DB: {sqle}")
                    
                    pb_src = conversations_path / f"{resume_session_id}.pb"
                    pb_dst = conversations_path / f"{new_session_id}.pb"
                    if pb_src.exists():
                        try:
                            pb_data = pb_src.read_bytes()
                            pb_data_updated = pb_data.replace(
                                resume_session_id.encode("utf-8"),
                                new_session_id.encode("utf-8")
                            )
                            pb_dst.write_bytes(pb_data_updated)
                            print(f"[AGY] Copied and updated protobuf file: {pb_dst.name}")
                        except Exception as pbe:
                            log.warning(f"[AGY] Failed to update protobuf file: {pbe}")
                
                resume_session_id = new_session_id
            except Exception as e:
                log.warning(f"[AGY] Failed to fork session: {e}")

    # Determine baseline before launching process to avoid race conditions
    import time as _time
    launch_ts = _time.time()

    use_resume = bool(resume_session_id)
    baseline_file = None
    baseline_time = launch_ts
    initial_size = 0

    target_session = resume_session_id or str(conv_id)
    expected_session_dir = brain_path / target_session

    if use_resume:
        explicit_baseline = expected_session_dir / ".system_generated" / "logs" / "transcript.jsonl"
        if explicit_baseline.exists():
            baseline_file = explicit_baseline
            try:
                initial_size = baseline_file.stat().st_size
                baseline_time = baseline_file.stat().st_mtime
            except OSError:
                pass

    if not baseline_file and use_resume:
        baseline_file, _temp_time = find_latest_transcript(expected_session_dir)
        if baseline_file:
            try:
                initial_size = baseline_file.stat().st_size
                baseline_time = baseline_file.stat().st_mtime
            except OSError:
                pass

    # Record existing brain directories to identify newly created ones in new conversations
    existing_dirs = set()
    if brain_path.exists():
        try:
            existing_dirs = {p.name for p in brain_path.iterdir() if p.is_dir()}
        except OSError:
            pass

    agy_exe = _find_agy_exe()

    # Windows has a ~32K char limit on command lines (CreateProcess).  For
    # prompts that approach this, write the full text to a temp file and pass a
    # short redirect instruction via -p instead.
    _MAX_CLI_PROMPT = 28_000  # leave headroom for the rest of the args
    prompt_file: Path | None = None

    if len(prompt) > _MAX_CLI_PROMPT:
        agents_dir = Path(cwd) / ".agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = agents_dir / f"loom_prompt_{conv_id}.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        cli_prompt = (
            f"Read the full conversation context from the file at "
            f"{prompt_file.name} in the .agents directory and respond "
            f"to the latest user message. Do NOT summarize the file — "
            f"treat its contents as the conversation history and reply naturally."
        )
        print(f"[AGY] Prompt too large for CLI ({len(prompt)} chars), "
              f"wrote to {prompt_file}")
    else:
        cli_prompt = prompt

    # Operator turns: do NOT pass --conversation. Each operator turn is a
    # self-contained scan/triage request; resuming the agy conversation
    # forces agy to use the tool registry baked in at conv-creation time
    # (so newly-registered nrol-ao MCP tools are invisible) AND lets agy's
    # own context fill up across turns until it auto-compacts on what feels
    # to the user like turn 1. Fresh conv per turn → fresh tool registry
    # read from .agents/mcp_config.json, no carry-over compaction.
    cc_args = []
    if not nrol_operator:
        cc_args += ["--conversation", resume_session_id or str(conv_id)]

    # Explicitly set the agy project ID so MCP config from .agents/ loads.
    # agy's default-project cache (cache/default_project_id.txt) gets clobbered
    # by neutral runs; without --project, new operator conversations fall into
    # the "default-cli-project" which has no MCP servers configured.
    if nrol_operator:
        project_id = _get_agy_project_id(Path(cwd))
        if project_id:
            cc_args += ["--project", project_id]

    cc_args += [
        "-p", cli_prompt,
        "--dangerously-skip-permissions",
        "--print-timeout", "60m",
    ]
    # NOTE: --sandbox is deliberately not passed for operator convs: bare
    # headless smoke runs hang with AND without the flag outside the Loom
    # harness (2026-06-11), so it cannot be validated. The PreToolUse hook
    # deny-list is the tool-blocking layer and does not depend on it
    # (--dangerously-skip-permissions only skips agy's own approvals, never
    # the hook — same combination backstage mode relies on).

    env = {
        **os.environ,
        "LOOM_CONV_ID": str(conv_id),
        "LOOM_PORT": str(server_port),
        "BASH_DEFAULT_TIMEOUT_MS": "1200000",  # 20 minutes (default is 2m)
        "BASH_MAX_TIMEOUT_MS": "3600000",      # 60 minutes (default is 10m)
    }
    if backstage_parent_id:
        env["LOOM_BACKSTAGE_PARENT_ID"] = str(backstage_parent_id)
    if nrol_operator:
        # cc_permission_hook denies Write/Edit/Bash/etc. without prompting
        # when this is set — agy's only tool-blocking surface.
        env["LOOM_NROL_OPERATOR"] = "1"

    # launch_ts already recorded above

    cmd = [agy_exe] + cc_args
    print(f"[AGY] CMD: {' '.join(cmd[:6])}{'...' if len(cmd) > 6 else ''}")
    print(f"[AGY] agy_model={agy_model}, prompt_len={len(prompt)}, cli_len={len(cli_prompt)}")

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

    # Drain stderr in the background (agy emits nothing on stderr, but we
    # must drain the pipe to avoid a blocked-subprocess deadlock on Windows).
    async def _read_stderr():
        try:
            async for line in proc.stderr:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    print(f"[AGY-stderr] {text}")
        except Exception as e:
            log.error(f"[AGY] Error reading stderr: {e}")
    asyncio.create_task(_read_stderr())

    # Clean up the temp prompt file when the process finishes
    if prompt_file:
        async def _cleanup_prompt_file():
            await proc.wait()
            try:
                prompt_file.unlink(missing_ok=True)
                print(f"[AGY] Cleaned up prompt file: {prompt_file}")
            except Exception as e:
                log.warning(f"[AGY] Failed to clean up prompt file: {e}")
        asyncio.create_task(_cleanup_prompt_file())

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
        if use_resume and baseline_file:
            active_file = baseline_file
        else:
            # Poll for the active transcript file being created or modified
            # Keep polling while the process is running, up to 60 seconds (1200 * 0.05s) max.
            polls = 0
            while True:
                latest_file = None
                latest_time = 0.0
                is_active = False

                # 1. Prefer expected_session_dir if it exists (resume/fork paths).
                if expected_session_dir.exists():
                    latest_file, latest_time = find_latest_transcript(expected_session_dir)
                    if latest_file:
                        if baseline_file:
                            if latest_file != baseline_file or latest_time > baseline_time + 0.1:
                                is_active = True
                            elif polls % 40 == 0:
                                print(
                                    f"[AGY] candidate transcript={latest_file} rejected: "
                                    f"mtime={latest_time:.3f} baseline={baseline_time:.3f}"
                                )
                        else:
                            if latest_time > launch_ts - 2.0:
                                is_active = True
                            elif polls % 40 == 0:
                                print(
                                    f"[AGY] candidate transcript={latest_file} rejected: "
                                    f"mtime={latest_time:.3f} launch_ts={launch_ts:.3f}"
                                )

                # 2. Fallback: find newly created folders (not in existing_dirs) for new conversations
                # where agy generates a new random UUID folder. Entered if no active file exists in expected_session_dir.
                if not is_active and brain_path.exists():
                    latest_file = None
                    latest_time = 0.0
                    try:
                        for p in brain_path.iterdir():
                            if not p.is_dir() or p.name in existing_dirs:
                                continue
                            fp, mtime = find_latest_transcript(p)
                            if fp and mtime > latest_time:
                                latest_file = fp
                                latest_time = mtime
                    except OSError:
                        pass

                    if latest_file:
                        if baseline_file:
                            if latest_file != baseline_file or latest_time > baseline_time + 0.1:
                                is_active = True
                            elif polls % 40 == 0:
                                print(
                                    f"[AGY] fallback candidate transcript={latest_file} rejected: "
                                    f"mtime={latest_time:.3f} baseline={baseline_time:.3f}"
                                )
                        else:
                            if latest_time > launch_ts - 2.0:
                                is_active = True
                            elif polls % 40 == 0:
                                print(
                                    f"[AGY] fallback candidate transcript={latest_file} rejected: "
                                    f"mtime={latest_time:.3f} launch_ts={launch_ts:.3f}"
                                )

                if is_active:
                    print(
                        f"[AGY] selected transcript={latest_file} "
                        f"mtime={latest_time:.3f} launch_ts={launch_ts:.3f} "
                        f"baseline_file={baseline_file} use_resume={use_resume} "
                        f"expected={expected_session_dir}"
                    )
                    active_file = latest_file
                    break

                # If the process is no longer running and we have polled for at least 2 seconds, stop
                if proc.returncode is not None and polls > 40:
                    break

                # Timeout safety limit (60 seconds)
                if polls > 1200:
                    break

                polls += 1
                await asyncio.sleep(0.05)

        if not active_file:
            log.error("[AGY] Timeout waiting for active transcript file. Waiting for process completion.")
            await proc.wait()
            # Post-completion scan: check if it was written during process shutdown
            latest_file = None
            latest_time = 0.0
            is_active = False
            if expected_session_dir.exists():
                latest_file, latest_time = find_latest_transcript(expected_session_dir)
                if latest_file:
                    if baseline_file:
                        if latest_file != baseline_file or latest_time > baseline_time + 0.1:
                            is_active = True
                    else:
                        if latest_time > launch_ts - 2.0:
                            is_active = True

            if not is_active and brain_path.exists():
                latest_file = None
                latest_time = 0.0
                try:
                    for p in brain_path.iterdir():
                        if not p.is_dir() or p.name in existing_dirs:
                            continue
                        fp, mtime = find_latest_transcript(p)
                        if fp and mtime > latest_time:
                            latest_file = fp
                            latest_time = mtime
                except OSError:
                    pass

                if latest_file:
                    if baseline_file:
                        if latest_file != baseline_file or latest_time > baseline_time + 0.1:
                            is_active = True
                    else:
                        if latest_time > launch_ts - 2.0:
                            is_active = True

            if is_active:
                active_file = latest_file
                print(f"[AGY] Found transcript post-completion: {active_file}")
            else:
                err = _scan_agy_log_for_error(launch_ts)
                if err:
                    print(f"[AGY] CLI log diagnosis: {err}")
                queue.put_nowait({
                    "type": "result",
                    "is_error": True if err else (proc.returncode != 0),
                    "result_text": "",
                    "error": err,
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
                    # Seek to where the file was before agy launched.
                    # JSONL files always end on a newline boundary, so
                    # initial_size lands right at the start of the next line
                    # (or at true EOF). Do NOT consume a readline — that
                    # would eat the first real event agy writes.
                    f.seek(initial_size)
                
                full_text = ""
                processed_steps = set()
                active_tool_calls = []
                _eof_polls = 0  # heartbeat counter for liveness signal

                while True:
                    line = f.readline()
                    if line:
                        _eof_polls = 0  # reset heartbeat on any content
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
                                        tool_id = f"sys-{step_index}"
                                        block_name = _determine_block_name(etype, content)
                                        queue.put_nowait({
                                            "type": "tool_start",
                                            "name": block_name,
                                            "tool_id": tool_id,
                                        })
                                        queue.put_nowait({
                                            "type": "tool_result",
                                            "content": content,
                                            "tool_id": tool_id,
                                            "is_error": False,
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
                                                tool_id = f"sys-{step_index}"
                                                block_name = _determine_block_name(etype, content)
                                                queue.put_nowait({
                                                    "type": "tool_start",
                                                    "name": block_name,
                                                    "tool_id": tool_id,
                                                })
                                                queue.put_nowait({
                                                    "type": "tool_result",
                                                    "content": content,
                                                    "tool_id": tool_id,
                                                    "is_error": False,
                                                })
                                except Exception as e:
                                    log.error(f"[AGY] Error parsing final transcript line: {e}")
                            break

                        # Clear EOF flag to allow reading new appends (buffered TextIOWrapper cache workaround)
                        try:
                            f.seek(f.tell())
                        except OSError:
                            pass

                        # Liveness heartbeat: every ~1.5s (30 polls × 0.05s)
                        # of no new transcript content, check if agy is alive
                        # by watching the CLI log mtime. agy writes to its log
                        # for every API call, so a changing mtime means activity
                        # even when the transcript hasn't flushed.
                        _eof_polls += 1
                        if _eof_polls % 30 == 0 and not full_text:
                            # Signal liveness if agy is still working
                            if _is_agy_alive(launch_ts):
                                elapsed = int(_time.time() - launch_ts)
                                queue.put_nowait({
                                    "type": "status",
                                    "text": f"Working ({elapsed}s) — agy is processing, waiting for step to complete...",
                                })
                            # Check for fatal errors
                            elapsed = int(_time.time() - launch_ts)
                            log_err = _scan_agy_log_for_error(launch_ts)
                            if log_err:
                                # Fatal error discovered — surface and abort.
                                print(f"[AGY] Live log diagnosis ({elapsed}s): {log_err}")
                                queue.put_nowait({
                                    "type": "status",
                                    "text": log_err,
                                })
                                # Give agy a moment to exit on its own,
                                # then force-terminate so we don't stall.
                                try:
                                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                                except asyncio.TimeoutError:
                                    proc.kill()
                                break

                        await asyncio.sleep(0.05)

                post_err = None
                if not full_text:
                    post_err = _scan_agy_log_for_error(launch_ts)
                    if post_err:
                        print(f"[AGY] CLI log diagnosis (post-transcript): {post_err}")
                queue.put_nowait({
                    "type": "result",
                    "is_error": bool(post_err) or proc.returncode != 0,
                    "result_text": full_text,
                    "error": post_err,
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
