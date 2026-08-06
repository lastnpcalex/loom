"""ChatGPT Codex app-server subprocess wrapper.

Runs Codex through the app-server JSONL protocol so Loom can broker live
approval requests instead of using non-interactive `codex exec`.
"""

import asyncio
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from loom_agent_prompt import prepend_loom_agent_context

log = logging.getLogger(__name__)

_IGNORED_APP_SERVER_EVENTS = {
    "account/rateLimits/updated",
    "remoteControl/status/changed",
    "serverRequest/resolved",
    "turn/started",
}

_IGNORED_APP_SERVER_ITEM_TYPES = {
    "agentMessage",
    "reasoning",
    "userMessage",
}


def _toml_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_literal(v) for v in values) + "]"


def _codex_config_args(key: str, value: str) -> list[str]:
    return ["-c", f"{key}={value}"]


def _nrol_mcp_config(conv_id: int, server_port: int, force: bool = False) -> dict:
    if not force and os.environ.get("NROL_AO_AUTO_MCP", "1") in {"0", "false", "False"}:
        return {}
    root = Path(__file__).parent
    nrol_repo = Path(os.environ.get("NROL_AO_REPO", r"C:\Claude-Code\NROL-AO\temp-repo"))
    nrol_server = root / "mcp_servers" / "nrol_ao" / "server.py"
    if not nrol_repo.exists() or not nrol_server.is_file():
        return {}

    env = {
        "NROL_AO_REPO": str(nrol_repo),
        **(
            {"NROL_AO_STATE_DIR": os.environ["NROL_AO_STATE_DIR"]}
            if os.environ.get("NROL_AO_STATE_DIR", "").strip()
            else {}
        ),
        "NROL_AO_ACTIVITY_DIR": os.environ.get(
            "NROL_AO_ACTIVITY_DIR",
            str(nrol_repo / "loom" / "mcp_activity"),
        ),
        "ALPHA_OMEGA_PORT": os.environ.get("ALPHA_OMEGA_PORT", "8098"),
        "LOOM_PORT": str(server_port),
        "LOOM_CONV_ID": str(conv_id),
        "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    try:
        from config import config as _loom_config

        env.setdefault("NROL_AO_LLAMA_HOST", _loom_config.llama_host_url())
        if getattr(_loom_config, "llama_model", ""):
            env.setdefault("NROL_AO_LLAMA_MODEL", _loom_config.llama_model)
    except Exception:
        pass

    return {
        "command": sys.executable,
        "args": ["-m", "mcp_servers.nrol_ao.server"],
        "env": env,
        "required": True,
        "startup_timeout_sec": 20.0,
        "tool_timeout_sec": 1200.0,
        "default_tools_approval_mode": "approve",
    }


def _web_tools_mcp_config() -> dict:
    """Keyless web_search/web_fetch (DuckDuckGo + trafilatura) for operators.

    Mirrors the claude_client web-tools registration: NROL operators read
    sources on the open web but codex has no Anthropic WebSearch/WebFetch.
    """
    script = Path(__file__).parent / "mcp_web_tools.py"
    if not script.is_file():
        return {}
    return {
        "command": sys.executable,
        "args": [str(script)],
        "startup_timeout_sec": 20.0,
        "tool_timeout_sec": 600.0,
        "default_tools_approval_mode": "approve",
    }


def _ensure_operator_instructions(workspace_root: Path) -> None:
    """Land the operator role rules where codex auto-loads them: AGENTS.md in cwd.

    The operator workspace is shared across operator conversations, so an
    idempotent overwrite keeps it current with OPERATOR.md. The app-server
    baseInstructions field is deliberately not used — it replaces codex's
    default instructions wholesale instead of adding to them.
    """
    operator_md = Path(__file__).parent / "mcp_servers" / "nrol_ao" / "OPERATOR.md"
    if operator_md.is_file():
        (workspace_root / "AGENTS.md").write_text(
            operator_md.read_text(encoding="utf-8"), encoding="utf-8"
        )


def _prepare_codex_prompt(
    prompt: str,
    backstage_parent_id: int | None = None,
    nrol_operator: bool = False,
) -> str:
    """Inject the Loom contract for ordinary Codex sessions."""
    if backstage_parent_id or nrol_operator:
        return prompt
    return prepend_loom_agent_context(prompt, "codex")


def _thread_mcp_servers(conv_id: int, server_port: int, nrol_operator: bool = False) -> dict:
    """MCP servers for a codex thread, keyed by server name.

    Operator threads get exactly nrol-ao + web-tools — the codex mirror of
    --strict-mcp-config. That holds because the thread config is the whole
    MCP surface only while ~/.codex/config.toml carries no mcp_servers of
    its own (true as of 2026-06-11); if user-scope servers ever appear they
    need explicit `enabled=false` overrides here.
    """
    servers: dict[str, dict] = {}
    nrol = _nrol_mcp_config(conv_id, server_port, force=nrol_operator)
    if nrol:
        servers["nrol-ao"] = nrol
    if nrol_operator:
        web = _web_tools_mcp_config()
        if web:
            servers["web-tools"] = web
    return servers


def _mcp_server_config_args(name: str, config: dict) -> list[str]:
    if not config:
        return []
    args: list[str] = []
    server_key = f'mcp_servers."{name}"'
    args += _codex_config_args(f"{server_key}.command", _toml_literal(config["command"]))
    args += _codex_config_args(f"{server_key}.args", _toml_array(config["args"]))
    if config.get("required"):
        args += _codex_config_args(f"{server_key}.required", "true")
    if "startup_timeout_sec" in config:
        args += _codex_config_args(f"{server_key}.startup_timeout_sec", str(config["startup_timeout_sec"]))
    if "tool_timeout_sec" in config:
        args += _codex_config_args(f"{server_key}.tool_timeout_sec", str(config["tool_timeout_sec"]))
    if "default_tools_approval_mode" in config:
        args += _codex_config_args(
            f"{server_key}.default_tools_approval_mode",
            _toml_literal(config["default_tools_approval_mode"]),
        )
    for key, value in config.get("env", {}).items():
        args += _codex_config_args(f"{server_key}.env.{key}", _toml_literal(value))
    return args


def _codex_tool_name(item: dict) -> str:
    """Return a stable Loom-facing name for Codex tool items."""
    item_type = item.get("type")
    if item_type in ("command_execution", "commandExecution", "local_shell_call", "localShellCall"):
        return "Bash"
    if item_type in ("file_change", "fileChange"):
        return "Edit"
    if item_type in ("mcp_tool_call", "mcpToolCall"):
        server = item.get("server") or "mcp"
        tool = item.get("tool") or item.get("toolName") or "tool"
        return f"{server}.{tool}"
    if item_type in ("dynamic_tool_call", "dynamicToolCall"):
        namespace = item.get("namespace")
        tool = item.get("tool") or item.get("toolName") or "tool"
        return f"{namespace}.{tool}" if namespace else tool
    return (
        item.get("name")
        or item.get("tool_name")
        or item.get("function_name")
        or item.get("call_name")
        or item_type
        or "tool"
    )


def _is_codex_tool_item(item: dict) -> bool:
    """True for Codex item types that should render as Loom tool blocks."""
    item_type = item.get("type")
    return item_type in {
        "mcp_tool_call",
        "mcpToolCall",
        "dynamic_tool_call",
        "dynamicToolCall",
        "command_execution",
        "commandExecution",
        "local_shell_call",
        "localShellCall",
        "function_call",
        "tool_call",
        "custom_tool_call",
        "file_change",
        "fileChange",
    } or any(key in item for key in ("tool_name", "function_name", "call_id"))


def _codex_item_id(item: dict, raw: dict) -> str:
    """Pick the most stable id available for matching start/result events."""
    return str(
        item.get("id")
        or item.get("call_id")
        or item.get("tool_call_id")
        or item.get("item_id")
        or raw.get("item_id")
        or raw.get("timestamp")
        or ""
    )


def _text_from_value(value) -> str:
    """Extract readable text from common Codex scalar/list/dict content shapes."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "".join(_text_from_value(part) for part in value)
    if isinstance(value, dict):
        for key in ("text", "content", "output_text", "message", "summary"):
            text = _text_from_value(value.get(key))
            if text:
                return text
        if "error" in value:
            return _text_from_value(value["error"])
        try:
            return json.dumps(value, indent=2)
        except TypeError:
            return str(value)
    return str(value)


def _codex_item_text(item: dict) -> str:
    """Extract assistant/reasoning text from current and likely Codex item shapes."""
    for key in ("text", "content", "output_text", "message", "summary"):
        text = _text_from_value(item.get(key))
        if text:
            return text
    return ""


def _codex_tool_input(item: dict) -> str:
    """Format Codex tool input for Loom's tool detail panel."""
    item_type = item.get("type")
    if item_type in ("command_execution", "commandExecution", "local_shell_call", "localShellCall"):
        command = item.get("command") or item.get("cmd") or item.get("script") or ""
        return json.dumps({"command": command}, indent=2) if command else ""
    if item_type in ("file_change", "fileChange"):
        changes = item.get("changes") or item.get("fileChanges") or []
        return json.dumps({"changes": changes}, indent=2)
    args = None
    for key in ("arguments", "input", "tool_input", "params", "parameters", "args"):
        if key in item:
            args = item.get(key)
            break
    if args is not None:
        if isinstance(args, str):
            return args
        try:
            return json.dumps(args, indent=2)
        except TypeError:
            return str(args)
    return ""


def _codex_tool_output(item: dict) -> str:
    """Extract Codex tool output regardless of item type."""
    if item.get("type") in ("file_change", "fileChange"):
        changes = item.get("changes") or item.get("fileChanges") or []
        try:
            return json.dumps(changes, indent=2)
        except TypeError:
            return str(changes)
    for key in ("aggregated_output", "formatted_output", "output", "result", "text", "content", "error"):
        value = item.get(key)
        if value is not None:
            return _text_from_value(value)
    return ""


def _codex_diff_payload(raw: dict) -> dict:
    params = raw.get("params") or {}
    diff = (
        params.get("diff")
        or params.get("unifiedDiff")
        or params.get("unified_diff")
        or params.get("patch")
        or raw.get("diff")
        or raw.get("patch")
        or ""
    )
    files = (
        params.get("files")
        or params.get("fileChanges")
        or params.get("changes")
        or raw.get("files")
        or raw.get("fileChanges")
        or raw.get("changes")
        or []
    )
    return {
        "kind": "codex_diff",
        "threadId": params.get("threadId") or raw.get("threadId"),
        "turnId": params.get("turnId") or raw.get("turnId"),
        "diff": diff,
        "files": files,
    }


def _codex_diff_tool_id(raw: dict) -> str:
    params = raw.get("params") or {}
    ident = (
        params.get("turnId")
        or params.get("threadId")
        or raw.get("turnId")
        or raw.get("threadId")
        or "thread"
    )
    return f"codex-diff:{ident}"


def _codex_runner_error(item: dict) -> str:
    output = _codex_tool_output(item)
    if "windows sandbox: timed out" in output or "runner pipe-in" in output:
        return "Codex Windows sandbox runner failed before the command started."
    return ""


def _codex_usage(raw: dict) -> dict | None:
    """Normalize token usage from turn/item payloads when Codex includes it."""
    params = raw.get("params") or {}
    candidates = [
        raw.get("usage"),
        raw.get("token_usage"),
        raw.get("tokenUsage"),
        raw.get("metrics", {}).get("usage") if isinstance(raw.get("metrics"), dict) else None,
        params.get("usage") if isinstance(params, dict) else None,
        params.get("token_usage") if isinstance(params, dict) else None,
        params.get("tokenUsage") if isinstance(params, dict) else None,
    ]
    for container_name in ("turn", "thread", "message", "item"):
        container = params.get(container_name) if isinstance(params, dict) else None
        if isinstance(container, dict):
            candidates.extend([
                container.get("usage"),
                container.get("token_usage"),
                container.get("tokenUsage"),
            ])
    usage = next((candidate for candidate in candidates if isinstance(candidate, dict)), None)
    if not usage:
        return None

    input_tokens = (
        usage.get("input_tokens")
        or usage.get("inputTokens")
        or usage.get("prompt_tokens")
        or usage.get("promptTokens")
        or usage.get("total_input_tokens")
        or usage.get("totalInputTokens")
        or 0
    )
    output_tokens = (
        usage.get("output_tokens")
        or usage.get("outputTokens")
        or usage.get("completion_tokens")
        or usage.get("completionTokens")
        or usage.get("total_output_tokens")
        or usage.get("totalOutputTokens")
        or 0
    )
    if not input_tokens and not output_tokens:
        return None
    return {
        "type": "usage",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _codex_status_payload(raw: dict, params: dict | None = None) -> dict:
    """Return the nested status-bearing payload from app-server notifications."""
    params = params or raw.get("params") or {}
    for container in (
        params.get("turn"),
        raw.get("turn"),
        params.get("thread"),
        raw.get("thread"),
        params,
        raw,
    ):
        if isinstance(container, dict):
            return container
    return {}


def _codex_status_value(raw: dict, params: dict | None = None) -> str:
    payload = _codex_status_payload(raw, params)
    value = (
        payload.get("status")
        or payload.get("state")
        or payload.get("phase")
        or payload.get("lifecycle")
        or ""
    )
    if isinstance(value, dict):
        value = (
            value.get("type")
            or value.get("status")
            or value.get("state")
            or value.get("phase")
            or value.get("kind")
            or ""
        )
    return str(value).lower()


def _codex_turn_id(raw: dict, params: dict | None = None) -> str:
    params = params or raw.get("params") or {}
    payload = _codex_status_payload(raw, params)
    return str(
        payload.get("id")
        or payload.get("turnId")
        or payload.get("turn_id")
        or raw.get("turnId")
        or raw.get("turn_id")
        or params.get("turnId")
        or params.get("turn_id")
        or ""
    )


def _codex_terminal_status(raw: dict, params: dict | None = None) -> tuple[bool, bool, str]:
    """Return (is_terminal, is_error, message) for status notifications."""
    params = params or raw.get("params") or {}
    etype = raw.get("type") or raw.get("method") or ""
    status = _codex_status_value(raw, params)
    if etype in ("thread/status/changed", "thread.status.changed"):
        if status in ("idle", "completed", "complete", "finished", "success", "succeeded"):
            return True, False, ""
        if status in ("failed", "error", "cancelled", "canceled", "interrupted"):
            return True, True, _text_from_value(
                params.get("error") or raw.get("error") or "Codex thread failed"
            )
    if etype in ("turn/status/changed", "turn.status.changed"):
        if status in ("completed", "complete", "finished", "success", "succeeded"):
            return True, False, ""
        if status in ("failed", "error", "cancelled", "canceled", "interrupted"):
            return True, True, _text_from_value(
                params.get("error") or raw.get("error") or "Codex turn failed"
            )
    return False, False, ""


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


def _codex_approval_policy(permission_mode: str | None) -> str:
    """Map Loom's permission selector to Codex's approval policy.

    Codex Plan mode is a collaboration mode, not an approval policy. Loom's
    app-server integration does not currently set that collaboration mode, so
    any legacy "plan" value is treated like the interactive default.
    """
    mode = (permission_mode or "default").lower()
    if mode in ("never", "none"):
        return "never"
    if mode in ("on-request", "request"):
        return "on-request"
    return "on-request"


def _codex_launch_policies(permission_mode: str | None, nrol_operator: bool = False) -> tuple[str, str]:
    """(approval_policy, sandbox_mode) for a codex launch.

    Operator threads: read-only sandbox with approvalPolicy=never, so write
    and escalation attempts fail instead of raising a clickable prompt.
    Codex cannot drop its shell, so the guarantee is "shell exists but
    cannot write"; posterior commits still raise their own Loom approval
    inside the nrol-ao MCP server.
    """
    if nrol_operator:
        return "never", "read-only"
    return _codex_approval_policy(permission_mode), "workspace-write"


def _codex_thread_request(
    cwd: str,
    codex_model: str,
    approval_policy: str,
    sandbox_mode: str,
    thread_config: dict | None = None,
    resume_session_id: str | None = None,
    fork_session: bool = False,
) -> tuple[str, dict]:
    """Build the app-server thread request method and params."""
    params = {
        "cwd": cwd,
        "model": codex_model,
        "approvalPolicy": approval_policy,
        "approvalsReviewer": "user",
        "sandbox": sandbox_mode,
    }
    if thread_config:
        params["config"] = thread_config

    if resume_session_id:
        params["threadId"] = resume_session_id
        return ("thread/fork" if fork_session else "thread/resume"), params

    params["sessionStartSource"] = "startup"
    params["threadSource"] = "user"
    return "thread/start", params


def _json_dumps_line(payload: dict) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def _compact_json(value, limit: int = 4000) -> str:
    try:
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def _app_user_input(prompt: str) -> list[dict]:
    return [{"type": "text", "text": prompt}]


def _app_sandbox_policy(cwd: str, nrol_operator: bool = False) -> dict:
    if nrol_operator:
        # Operators read sources but never write through the shell.
        return {"type": "readOnly"}
    return {
        "type": "workspaceWrite",
        "writableRoots": [cwd],
        "networkAccess": False,
    }


def _codex_goal_set_params(
    thread_id: str,
    objective: str | None = None,
    status: str | None = None,
    token_budget: int | None = None,
) -> dict:
    params = {"threadId": thread_id}
    if objective is not None:
        params["objective"] = objective
    if status is not None:
        params["status"] = status
    if token_budget is not None:
        params["tokenBudget"] = token_budget
    return params


def _codex_goal_from_response(response: dict) -> dict | None:
    result = response.get("result") if isinstance(response, dict) else None
    if not isinstance(result, dict):
        return None
    goal = result.get("goal")
    return goal if isinstance(goal, dict) else None


def _app_permission_payload(method: str, params: dict) -> tuple[str, dict]:
    if method == "item/commandExecution/requestApproval":
        command = params.get("command") or params.get("cmd") or params.get("argv") or params.get("execCommand")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        return "Bash", {"command": command or "", **params}
    if method == "item/fileChange/requestApproval":
        return "Edit", params
    if method == "item/permissions/requestApproval":
        return "PermissionRequest", params
    if method == "item/tool/requestUserInput":
        return params.get("toolName") or params.get("tool") or "ToolInput", params
    return method, params


def _requested_permissions(params: dict) -> dict:
    for key in ("permissions", "requestedPermissions", "requested_permissions"):
        value = params.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _app_approval_response(method: str, allow: bool, always: bool, params: dict | None = None) -> dict:
    params = params or {}
    decision = "acceptForSession" if allow and always else ("accept" if allow else "decline")
    if method == "item/permissions/requestApproval":
        if allow:
            return {
                "permissions": _requested_permissions(params),
                "scope": "session" if always else "turn",
            }
        return {
            "permissions": {
                "filesystem": {"entries": []},
                "network": {"enabled": False},
            },
            "scope": "turn",
            "strictAutoReview": True,
        }
    if method == "item/tool/requestUserInput":
        return {"decision": decision}
    return {"decision": decision}


async def _post_loom_permission(
    server_port: int,
    conv_id: int,
    method: str,
    params: dict,
    permission_scope: str = "",
    permission_request_handler=None,
) -> dict:
    tool_name, tool_input = _app_permission_payload(method, params)
    payload = {
        "loom_conv_id": conv_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "source": "codex_app_server",
        "approval_method": method,
        "permission_scope": permission_scope,
    }
    if permission_request_handler is not None:
        try:
            return await permission_request_handler(payload)
        except Exception as exc:
            log.exception("[CODEX] Direct Loom permission handler failed")
            return {"allow": False, "message": f"Loom permission handler failed: {exc}"}

    def _post() -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{server_port}/api/cc-permission",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=86400) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
        except urllib.error.URLError as exc:
            return {"allow": False, "message": f"Loom permission bridge failed: {exc}"}

    return await asyncio.to_thread(_post)


async def manage_codex_goal(
    action: str,
    cwd: str,
    conv_id: int = 0,
    server_port: int = 8000,
    model: str = "Codex (GPT-4o)",
    permission_mode: str = "default",
    resume_session_id: str | None = None,
    objective: str | None = None,
    status: str | None = None,
    token_budget: int | None = None,
    nrol_operator: bool = False,
) -> dict:
    """Run a short app-server control session for Codex thread goal commands."""
    workspace_root = Path(cwd).resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    cwd = str(workspace_root)

    codex_model = _loom_model_to_codex(model)
    codex_exe = _find_codex_exe()
    approval_policy, sandbox_mode = _codex_launch_policies(permission_mode, nrol_operator)
    mcp_servers_cfg = _thread_mcp_servers(conv_id, server_port, nrol_operator)
    mcp_args = [
        arg
        for name, server_cfg in mcp_servers_cfg.items()
        for arg in _mcp_server_config_args(name, server_cfg)
    ]
    cmd = [codex_exe, "app-server", *mcp_args, "--stdio", "--disable", "hooks"]
    env = {
        **os.environ,
        "LOOM_CONV_ID": str(conv_id),
        "LOOM_PORT": str(server_port),
        "LOOM_API_URL": f"http://127.0.0.1:{server_port}",
        "LOOM_WORKSPACE_ROOT": cwd,
    }
    if nrol_operator:
        env["LOOM_NROL_OPERATOR"] = "1"

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000 | 0x00000200

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
        **kwargs,
    )

    stderr_lines: list[str] = []
    pending_responses: dict[str | int, asyncio.Future] = {}
    request_id = 0

    async def _read_stderr():
        try:
            async for line in proc.stderr:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    stderr_lines.append(text)
                    print(f"[CODEX-stderr] {text}")
        except Exception as e:
            log.error(f"[CODEX] Error reading goal stderr: {e}")

    stderr_task = asyncio.create_task(_read_stderr())

    async def _read_stdout():
        try:
            async for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    print(f"[CODEX] Non-JSON goal line on stdout: {line[:200]}")
                    continue
                if "id" in raw and ("result" in raw or "error" in raw) and "method" not in raw:
                    fut = pending_responses.pop(raw.get("id"), None)
                    if fut and not fut.done():
                        fut.set_result(raw)
        except Exception as e:
            for fut in pending_responses.values():
                if not fut.done():
                    fut.set_exception(RuntimeError(f"Codex app-server stdout failed: {e}"))

    stdout_task = asyncio.create_task(_read_stdout())

    async def _send(payload: dict):
        if not proc.stdin or proc.stdin.is_closing():
            raise RuntimeError("Codex app-server stdin is closed")
        proc.stdin.write(_json_dumps_line(payload))
        await proc.stdin.drain()

    async def _request(method: str, params: dict | None = None) -> dict:
        nonlocal request_id
        request_id += 1
        rid = request_id
        fut = asyncio.get_running_loop().create_future()
        pending_responses[rid] = fut
        payload = {"id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        await _send(payload)
        return await asyncio.wait_for(fut, timeout=120)

    cleaned_up = False

    async def _cleanup():
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        if proc.returncode is None:
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        if not stdout_task.done():
            stdout_task.cancel()
        try:
            await stdout_task
        except asyncio.CancelledError:
            pass
        await stderr_task

    try:
        init = await _request(
            "initialize",
            {
                "clientInfo": {"name": "Loom", "version": "1"},
                "capabilities": {"experimental": True, "experimentalApi": True},
            },
        )
        if init.get("error"):
            raise RuntimeError(json.dumps(init["error"]))
        await _send({"method": "initialized"})

        method, thread_params = _codex_thread_request(
            cwd,
            codex_model,
            approval_policy,
            sandbox_mode,
            {"mcp_servers": mcp_servers_cfg} if mcp_servers_cfg else None,
            resume_session_id,
            fork_session=False,
        )
        thread_start = await _request(method, thread_params)
        if thread_start.get("error"):
            raise RuntimeError(json.dumps(thread_start["error"]))

        thread_result = thread_start.get("result") or {}
        thread = thread_result.get("thread") or {}
        thread_id = thread.get("id") or thread.get("threadId") or resume_session_id
        if not thread_id:
            raise RuntimeError("Codex app-server did not return a thread id")

        normalized = (action or "get").lower()
        if normalized == "get":
            goal_response = await _request("thread/goal/get", {"threadId": thread_id})
        elif normalized == "clear":
            goal_response = await _request("thread/goal/clear", {"threadId": thread_id})
        elif normalized == "set":
            goal_response = await _request(
                "thread/goal/set",
                _codex_goal_set_params(
                    thread_id,
                    objective=objective,
                    status=status,
                    token_budget=token_budget,
                ),
            )
        else:
            raise ValueError(f"Unsupported Codex goal action: {action}")

        if goal_response.get("error"):
            raise RuntimeError(json.dumps(goal_response["error"]))
        return {
            "action": normalized,
            "thread_id": thread_id,
            "goal": _codex_goal_from_response(goal_response),
            "raw": goal_response.get("result") or {},
        }
    finally:
        await _cleanup()


async def run_codex(prompt: str, cwd: str, conv_id: int = 0, server_port: int = 8000,
                    model: str = "Codex (GPT-4o)", effort: str = "high",
                    permission_mode: str = "default",
                    resume_session_id: str = None, fork_session: bool = False,
                    backstage_parent_id: int | None = None,
                    nrol_operator: bool = False,
                    permission_request_handler=None,
                    codex_goal: dict | None = None):
    """Launch Codex app-server and yield Loom-compatible stream events."""
    workspace_root = Path(cwd).resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    cwd = str(workspace_root)

    codex_model = _loom_model_to_codex(model)
    codex_exe = _find_codex_exe()
    approval_policy, sandbox_mode = _codex_launch_policies(permission_mode, nrol_operator)
    gen_key = getattr(asyncio.current_task(), "_gen_key", None)
    permission_scope = f"gen:{gen_key[2]}" if gen_key else ""
    if permission_mode == "plan":
        plan_instruction = (
            "You are running in PLAN MODE. Your task is to analyze the codebase and write a comprehensive "
            "implementation plan to `implementation_plan.md` in the workspace. Do NOT modify any other files "
            "or run commands that modify the repository. Once the plan is written, present it to the user "
            "and ask for their approval. After writing the plan, end your turn immediately without performing "
            "any edits."
        )
        prompt = f"{plan_instruction}\n\n{prompt}"

    if nrol_operator:
        _ensure_operator_instructions(workspace_root)
    elif not backstage_parent_id:
        prompt = _prepare_codex_prompt(prompt, backstage_parent_id, nrol_operator)
    mcp_servers_cfg = _thread_mcp_servers(conv_id, server_port, nrol_operator)
    mcp_args = [
        arg
        for name, server_cfg in mcp_servers_cfg.items()
        for arg in _mcp_server_config_args(name, server_cfg)
    ]
    cmd = [codex_exe, "app-server", *mcp_args, "--stdio", "--disable", "hooks"]
    print(f"[CODEX] CMD: {' '.join(cmd)}")
    print(
        f"[CODEX] app-server model={codex_model}, approval={approval_policy}, "
        f"sandbox={sandbox_mode}, cwd={cwd}, prompt_len={len(prompt)}"
    )
    launch_info = {
        "type": "codex_launch_info",
        "surface": "app-server",
        "model": codex_model,
        "approval_policy": approval_policy,
        "approvals_reviewer": "user",
        "sandbox": sandbox_mode,
        "cwd": cwd,
        "writable_roots": [] if nrol_operator else [cwd],
        "hook_path": None,
        "hook_scope": "disabled",
        "mcp_servers": list(mcp_servers_cfg),
    }

    env = {
        **os.environ,
        "LOOM_CONV_ID": str(conv_id),
        "LOOM_PORT": str(server_port),
        "LOOM_API_URL": f"http://127.0.0.1:{server_port}",
        "LOOM_WORKSPACE_ROOT": cwd,
    }
    if backstage_parent_id:
        env["LOOM_BACKSTAGE_PARENT_ID"] = str(backstage_parent_id)
    if nrol_operator:
        env["LOOM_NROL_OPERATOR"] = "1"

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

    stderr_lines: list[str] = []

    async def _read_stderr():
        try:
            async for line in proc.stderr:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    stderr_lines.append(text)
                    print(f"[CODEX-stderr] {text}")
        except Exception as e:
            log.error(f"[CODEX] Error reading stderr: {e}")
    stderr_task = asyncio.create_task(_read_stderr())

    pending_responses: dict[str | int, asyncio.Future] = {}
    app_messages: asyncio.Queue[dict] = asyncio.Queue()
    request_id = 0

    async def _read_stdout():
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
                if "id" in raw and ("result" in raw or "error" in raw) and "method" not in raw:
                    fut = pending_responses.pop(raw.get("id"), None)
                    if fut and not fut.done():
                        fut.set_result(raw)
                    else:
                        await app_messages.put(raw)
                else:
                    await app_messages.put(raw)
        except Exception as e:
            await app_messages.put({"type": "error", "message": f"Codex app-server stdout failed: {e}"})

    stdout_task = asyncio.create_task(_read_stdout())

    async def _send(payload: dict):
        if not proc.stdin or proc.stdin.is_closing():
            raise RuntimeError("Codex app-server stdin is closed")
        proc.stdin.write(_json_dumps_line(payload))
        await proc.stdin.drain()

    async def _request(method: str, params: dict | None = None) -> dict:
        nonlocal request_id
        request_id += 1
        rid = request_id
        fut = asyncio.get_running_loop().create_future()
        pending_responses[rid] = fut
        payload = {"id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        await _send(payload)
        return await fut

    async def _answer_server_request(raw: dict):
        method = raw.get("method") or ""
        rid = raw.get("id")
        params = raw.get("params") or {}
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "item/tool/requestUserInput",
        }:
            tool_name, _ = _app_permission_payload(method, params)
            print(f"[CODEX] Waiting for Loom permission: method={method} tool={tool_name} request_id={rid}")
            print(f"[CODEX] Approval request params: {_compact_json(params)}")
            result = await _post_loom_permission(
                server_port,
                conv_id,
                method,
                params,
                permission_scope=permission_scope,
                permission_request_handler=permission_request_handler,
            )
            allow = bool(result.get("allow"))
            always = bool(result.get("always"))
            print(f"[CODEX] Loom permission resolved: method={method} allow={allow} always={always} request_id={rid}")
            response = _app_approval_response(method, allow, always, params)
            print(f"[CODEX] Approval response payload: {_compact_json(response)}")
            await _send({"id": rid, "result": response})
            return
        await _send({"id": rid, "error": {"code": -32601, "message": f"Loom does not handle {method}"}})

    cleaned_up = False

    async def _cleanup():
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        if proc.returncode is None:
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        if not stdout_task.done():
            stdout_task.cancel()
        try:
            await stdout_task
        except asyncio.CancelledError:
            pass
        await stderr_task

    async def _event_stream():
        session_id = resume_session_id or str(conv_id)
        full_text = ""
        got_result = False
        turn_inflight = False
        turn_activity_seen = False
        active_turn_id = ""
        last_usage_input_tokens = 0
        last_usage_output_tokens = 0
        started_tool_ids: set[str] = set()
        diff_tool_ids: set[str] = set()
        unknown_event_types: set[str] = set()
        unknown_item_types: set[str] = set()
        yield launch_info

        try:
            try:
                init = await _request(
                    "initialize",
                    {
                        "clientInfo": {"name": "Loom", "version": "1"},
                        "capabilities": {"experimental": True},
                    },
                )
                if init.get("error"):
                    await _cleanup()
                    yield {"type": "error", "message": json.dumps(init["error"])}
                    return
                await _send({"method": "initialized"})

                method, thread_params = _codex_thread_request(
                    cwd,
                    codex_model,
                    approval_policy,
                    sandbox_mode,
                    {"mcp_servers": mcp_servers_cfg} if mcp_servers_cfg else None,
                    resume_session_id,
                    fork_session,
                )

                thread_start = await _request(
                    method,
                    thread_params,
                )
                if thread_start.get("error"):
                    await _cleanup()
                    yield {"type": "error", "message": json.dumps(thread_start["error"])}
                    return

                thread_result = thread_start.get("result") or {}
                thread = thread_result.get("thread") or {}
                session_id = thread.get("id") or thread.get("threadId") or session_id
                yield {
                    "type": "session_info",
                    "session_id": session_id,
                    "model": thread_result.get("model") or codex_model,
                }
                if "nrol-ao" in mcp_servers_cfg:
                    yield {
                        "type": "status",
                        "text": "NROL MCP configured for this Codex thread; waiting for startup status",
                    }
                if codex_goal and codex_goal.get("objective"):
                    try:
                        goal_set = await _request(
                            "thread/goal/set",
                            _codex_goal_set_params(
                                session_id,
                                objective=codex_goal.get("objective"),
                                status=codex_goal.get("status") or "active",
                                token_budget=codex_goal.get("tokenBudget"),
                            ),
                        )
                        if goal_set.get("error"):
                            yield {
                                "type": "status",
                                "text": f"Codex goal sync failed: {json.dumps(goal_set['error'])}",
                            }
                        else:
                            yield {
                                "type": "codex_goal",
                                "goal": _codex_goal_from_response(goal_set),
                            }
                    except Exception as goal_exc:
                        yield {"type": "status", "text": f"Codex goal sync failed: {goal_exc}"}

                turn_start = await _request(
                    "turn/start",
                    {
                        "threadId": session_id,
                        "input": _app_user_input(prompt),
                        "cwd": cwd,
                        "model": codex_model,
                        "approvalPolicy": approval_policy,
                        "approvalsReviewer": "user",
                        "sandboxPolicy": _app_sandbox_policy(cwd, nrol_operator),
                        "effort": effort if effort in ("minimal", "low", "medium", "high", "xhigh") else None,
                    },
                )
                if turn_start.get("error"):
                    await _cleanup()
                    yield {"type": "error", "message": json.dumps(turn_start["error"])}
                    return
                turn_result = turn_start.get("result") or {}
                turn = turn_result.get("turn") or {}
                active_turn_id = str(
                    turn.get("id")
                    or turn.get("turnId")
                    or turn_result.get("turnId")
                    or turn_result.get("turn_id")
                    or ""
                )
                turn_inflight = True
            except Exception as e:
                await _cleanup()
                yield {"type": "error", "message": f"Codex app-server launch failed: {e}"}
                return

            while True:
                raw = await app_messages.get()
                method = raw.get("method")
                if method and "id" in raw:
                    asyncio.create_task(_answer_server_request(raw))
                    continue

                params = raw.get("params") or {}
                etype = raw.get("type") or method
                usage_evt = _codex_usage(raw)
                if usage_evt:
                    turn_activity_seen = True
                    current_input = int(usage_evt.get("input_tokens") or 0)
                    current_output = int(usage_evt.get("output_tokens") or 0)
                    usage_evt["input_tokens"] = current_input
                    if current_output >= last_usage_output_tokens:
                        usage_evt["output_tokens"] = current_output - last_usage_output_tokens
                    else:
                        usage_evt["output_tokens"] = current_output
                    last_usage_input_tokens = current_input or last_usage_input_tokens
                    last_usage_output_tokens = current_output or last_usage_output_tokens
                    yield usage_evt

                if etype in ("turn.started", "turn/started"):
                    event_turn_id = _codex_turn_id(raw, params)
                    if not active_turn_id and event_turn_id:
                        active_turn_id = event_turn_id
                    if not event_turn_id or not active_turn_id or event_turn_id == active_turn_id:
                        turn_activity_seen = True

                elif etype in ("thread.started", "thread/started"):
                    thread = params.get("thread") or raw.get("thread") or {}
                    session_id = raw.get("thread_id") or params.get("threadId") or thread.get("id") or session_id
                    yield {
                        "type": "session_info",
                        "session_id": session_id,
                        "model": codex_model,
                    }

                elif etype in (
                    "thread/status/changed",
                    "thread.status.changed",
                    "turn/status/changed",
                    "turn.status.changed",
                ):
                    terminal, is_error, err_msg = _codex_terminal_status(raw, params)
                    event_turn_id = _codex_turn_id(raw, params)
                    if (
                        terminal
                        and turn_inflight
                        and (not active_turn_id or not event_turn_id or event_turn_id == active_turn_id)
                        and (etype.startswith("turn") or turn_activity_seen)
                    ):
                        got_result = True
                        await _cleanup()
                        yield {
                            "type": "result",
                            "is_error": is_error,
                            "result_text": full_text,
                            "session_id": session_id,
                            "error": err_msg if is_error else "",
                        }
                        break
                    status = _codex_status_value(raw, params)
                    if status and status not in {"idle", "running", "in_progress", "active"}:
                        yield {"type": "status", "text": f"Codex status: {status}"}

                elif etype in ("thread/diff/updated", "thread.diff.updated", "turn/diff/updated", "turn.diff.updated"):
                    turn_activity_seen = True
                    tool_id = _codex_diff_tool_id(raw)
                    payload = _codex_diff_payload(raw)
                    if tool_id not in diff_tool_ids:
                        diff_tool_ids.add(tool_id)
                        started_tool_ids.add(tool_id)
                        yield {
                            "type": "tool_start",
                            "name": "Edit",
                            "tool_id": tool_id,
                        }
                        yield {
                            "type": "tool_input_delta",
                            "json": json.dumps(payload, indent=2),
                            "tool_id": tool_id,
                        }
                    yield {
                        "type": "tool_result",
                        "content": json.dumps(payload, indent=2),
                        "tool_id": tool_id,
                        "is_error": False,
                    }

                elif etype in ("mcpServer/startupStatus/updated", "mcpServer.startupStatus.updated"):
                    name = params.get("name") or raw.get("name") or "mcp"
                    status = params.get("status") or raw.get("status") or "unknown"
                    error = params.get("error") or raw.get("error")
                    text = f"MCP startup: {name} {status}"
                    if error:
                        text += f" ({error})"
                    yield {"type": "status", "text": text}

                elif etype in ("item.started", "item/started"):
                    turn_activity_seen = True
                    item = raw.get("item") or params.get("item") or params
                    item_type = item.get("type")
                    item_id = _codex_item_id(item, raw)
                    if _is_codex_tool_item(item):
                        tool_input = _codex_tool_input(item)
                        started_tool_ids.add(item_id)
                        yield {
                            "type": "tool_start",
                            "name": _codex_tool_name(item),
                            "tool_id": item_id,
                        }
                        if tool_input:
                            yield {
                                "type": "tool_input_delta",
                                "json": tool_input,
                                "tool_id": item_id,
                            }
                    elif item_type in _IGNORED_APP_SERVER_ITEM_TYPES:
                        pass
                    elif item_type and item_type not in unknown_item_types:
                        unknown_item_types.add(item_type)
                        print(f"[CODEX] Ignoring item.started type={item_type}: {json.dumps(item, default=str)[:500]}")

                elif etype in ("item.completed", "item/completed"):
                    turn_activity_seen = True
                    item = raw.get("item") or params.get("item") or params
                    item_type = item.get("type")
                    item_id = _codex_item_id(item, raw)
                    content = _codex_item_text(item)

                    if item_type in ("reasoning", "reasoningSummary", "reasoning_summary", "summary") and content:
                        chunk_size = 128
                        for i in range(0, len(content), chunk_size):
                            yield {"type": "thinking_delta", "text": content[i:i+chunk_size]}
                            await asyncio.sleep(0.005)

                    elif (
                        item_type in ("agent_message", "agentMessage", "message", "assistant_message", "assistantMessage", "output_text")
                        or item.get("role") == "assistant"
                    ) and content:
                        if not full_text:
                            chunk_size = 64
                            for i in range(0, len(content), chunk_size):
                                yield {"type": "text_delta", "text": content[i:i+chunk_size]}
                                await asyncio.sleep(0.01)
                            full_text += content

                    elif _is_codex_tool_item(item):
                        if item_id not in started_tool_ids:
                            started_tool_ids.add(item_id)
                            tool_input = _codex_tool_input(item)
                            yield {
                                "type": "tool_start",
                                "name": _codex_tool_name(item),
                                "tool_id": item_id,
                            }
                            if tool_input:
                                yield {
                                    "type": "tool_input_delta",
                                    "json": tool_input,
                                    "tool_id": item_id,
                                }
                        yield {
                            "type": "tool_result",
                            "content": _codex_tool_output(item),
                            "tool_id": item_id,
                            "is_error": item.get("status") == "failed" or item.get("exit_code") not in (None, 0),
                        }
                        runner_error = _codex_runner_error(item)
                        if runner_error:
                            yield {"type": "error", "message": runner_error}
                    elif item_type in _IGNORED_APP_SERVER_ITEM_TYPES:
                        pass
                    elif item_type and item_type not in unknown_item_types:
                        unknown_item_types.add(item_type)
                        print(f"[CODEX] Ignoring item.completed type={item_type}: {json.dumps(item, default=str)[:500]}")

                elif etype in ("item.updated", "item.delta", "item/agentMessage/delta", "item/reasoning/delta"):
                    turn_activity_seen = True
                    item = raw.get("item") or params.get("item") or {}
                    delta = (
                        raw.get("delta")
                        or params.get("delta")
                        or item.get("delta")
                        or raw.get("text")
                        or params.get("text")
                        or raw.get("content")
                    )
                    item_type = item.get("type") or raw.get("item_type") or (
                        "reasoning" if etype == "item/reasoning/delta" else "agentMessage"
                    )
                    text = _text_from_value(delta)
                    if item_type in ("reasoning", "reasoningSummary", "reasoning_summary", "summary") and text:
                        yield {"type": "thinking_delta", "text": text}
                    elif item_type in ("agent_message", "agentMessage", "message", "assistant_message", "assistantMessage", "output_text") and text:
                        full_text += text
                        yield {"type": "text_delta", "text": text}

                elif etype in ("turn.completed", "turn/completed"):
                    turn = params.get("turn") or raw.get("turn") or {}
                    for item in turn.get("items") or []:
                        item_id = _codex_item_id(item, raw)
                        content = _codex_item_text(item)
                        if _is_codex_tool_item(item):
                            if item_id not in started_tool_ids:
                                started_tool_ids.add(item_id)
                                yield {
                                    "type": "tool_start",
                                    "name": _codex_tool_name(item),
                                    "tool_id": item_id,
                                }
                            yield {
                                "type": "tool_result",
                                "content": _codex_tool_output(item),
                                "tool_id": item_id,
                                "is_error": item.get("status") == "failed" or item.get("exit_code") not in (None, 0),
                            }
                            runner_error = _codex_runner_error(item)
                            if runner_error:
                                yield {"type": "error", "message": runner_error}
                        elif (
                            item.get("type") in ("agent_message", "agentMessage", "message", "assistant_message", "assistantMessage", "output_text")
                            or item.get("role") == "assistant"
                        ) and content and not full_text:
                            full_text += content
                            yield {"type": "text_delta", "text": content}
                    got_result = True
                    await _cleanup()
                    yield {
                        "type": "result",
                        "is_error": False,
                        "result_text": full_text,
                        "session_id": session_id,
                    }
                    break

                elif etype in ("turn.failed", "turn/failed", "error", "warning"):
                    got_result = True
                    error = raw.get("error") or params.get("error") or {}
                    err_msg = error.get("message") if isinstance(error, dict) else error
                    err_msg = err_msg or raw.get("message") or params.get("message") or "Unknown error"
                    await _cleanup()
                    yield {
                        "type": "result",
                        "is_error": True,
                        "result_text": full_text,
                        "session_id": session_id,
                        "error": err_msg,
                    }
                    break

                elif etype in _IGNORED_APP_SERVER_EVENTS:
                    pass
                elif etype not in unknown_event_types:
                    unknown_event_types.add(str(etype))
                    print(f"[CODEX] Ignoring event type={etype}: {json.dumps(raw, default=str)[:500]}")

        except Exception as e:
            log.error(f"[CODEX] Error processing event stream: {e}")
            await _cleanup()
            yield {
                "type": "result",
                "is_error": True,
                "result_text": full_text,
                "session_id": session_id,
                "error": str(e),
            }

        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        if proc.returncode is None:
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        if not stdout_task.done():
            stdout_task.cancel()
        try:
            await stdout_task
        except asyncio.CancelledError:
            pass
        await stderr_task

        # Emit default result if not already yielded
        if not got_result:
            err_text = "\n".join(stderr_lines[-20:])
            yield {
                "type": "result",
                "is_error": proc.returncode != 0,
                "result_text": full_text or err_text,
                "session_id": session_id,
                "error": err_text if proc.returncode != 0 else "",
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
