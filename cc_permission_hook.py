#!/usr/bin/env python3
"""PreToolUse / BeforeTool hook -> Loom HTTP bridge.

Works with both Claude Code (PreToolUse) and Antigravity/agy (PreToolUse).
Routes tool permission decisions through Loom's HTTP API so the user
can approve/deny in the browser UI.

Claude Code output format:
  {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow/deny", ...}}

Antigravity (agy) output format:
  {"decision": "allow/deny", "reason": "..."}

Codex PermissionRequest output format:
  {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow/deny", ...}}}

Environment variables (set by Loom when launching CC/agy):
  LOOM_PORT: Port of the Loom server (default: 3000)
  LOOM_CONV_ID: Conversation ID in Loom
"""
import sys
import json
import os
import ssl
import urllib.request
import urllib.error


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
        "apply_patch": "apply_patch",
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
    elif name == "apply_patch":
        mapped["command"] = cleaned.get("command", cleaned.get("patch", ""))
    elif name in ("grep_search", "grep"):
        mapped["query"] = cleaned.get("Query", cleaned.get("query", ""))
        mapped["dir"] = cleaned.get("SearchPath", cleaned.get("dir", ""))
    else:
        mapped = cleaned
    return mapped

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# Tools that don't need user permission (read-only or low-risk)
READ_ONLY = {
    # Claude Code tools (CamelCase)
    "Read", "Glob", "Grep", "WebSearch", "WebFetch", "Task",
    "TaskGet", "TaskList", "TaskUpdate", "TaskCreate", "TaskStop",
    "TaskOutput", "TodoWrite", "Skill",
    "EnterPlanMode", "EnterWorktree", "ExitWorktree",
    "Explore", "CronList", "ToolSearch", "Agent",
    # Antigravity (agy) tools (snake_case)
    "read_file", "read_many_files", "glob", "grep_search",
    "list_directory", "google_web_search", "web_fetch",
    "write_todos", "enter_plan_mode",
    "cli_help", "codebase_investigator", "get_internal_docs",
    # MCP web-tools (keyless DuckDuckGo search for local models)
    "web_search",
}

AGY_TOOLS = {
    "view_file", "read_file", "read_many_files",
    "write_to_file", "write_file",
    "replace_file_content", "multi_replace_file_content",
    "run_command", "execute_command", "run_shell_command",
    "grep_search", "list_dir", "list_directory", "glob",
    "search_web", "google_web_search",
    "read_url_content", "web_fetch"
}


def allow(reason="Auto-approved", event_name="PreToolUse"):
    """Output allow JSON and exit 0."""
    sys.stderr.write(f"[Hook] Allowing: {reason}\n")
    if event_name == "BeforeTool":
        output = {
            "decision": "allow",
            "reason": reason
        }
    elif event_name == "PermissionRequest":
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {
                    "behavior": "allow"
                },
            }
        }
    else:
        output = {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "permissionDecision": "allow",
                "permissionDecisionReason": reason,
            }
        }
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()
    sys.exit(0)


def deny(reason="Blocked", event_name="PreToolUse"):
    """Output deny JSON and exit."""
    sys.stderr.write(f"[Hook] Denying: {reason}\n")
    if event_name == "BeforeTool":
        output = {
            "decision": "deny",
            "reason": reason
        }
    elif event_name == "PermissionRequest":
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {
                    "behavior": "deny",
                    "message": reason,
                },
            }
        }
    else:
        output = {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()
    sys.exit(0)


def main():
    event_name = "PreToolUse"
    if "--event" in sys.argv:
        try:
            idx = sys.argv.index("--event")
            if idx + 1 < len(sys.argv):
                event_name = sys.argv[idx + 1]
        except ValueError:
            pass
    elif len(sys.argv) > 1 and sys.argv[1] in ("PreToolUse", "PermissionRequest", "PostToolUse", "BeforeTool"):
        event_name = sys.argv[1]

    sys.stderr.write(f"[PERM-HOOK] Hook started for event {event_name}\n")
    port = os.environ.get("LOOM_PORT", "3000")
    conv_id = os.environ.get("LOOM_CONV_ID", "")
    backstage_parent = os.environ.get("LOOM_BACKSTAGE_PARENT_ID", "")
    nrol_operator = os.environ.get("LOOM_NROL_OPERATOR", "")
    sys.stderr.write(f"[PERM-HOOK] port={port} conv={conv_id} backstage={backstage_parent} nrol={nrol_operator}\n")

    if not conv_id:
        # Not running under Loom — pass through
        sys.exit(0)

    # Read tool info from stdin
    try:
        raw = sys.stdin.read()
        request = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, IOError):
        sys.exit(0)

    # Detect provider from hook_event_name if event_name argument is not passed (legacy)
    if "hook_event_name" in request:
        event_name = request["hook_event_name"]

    raw_tool = request.get("tool")
    tool_name = (
        request.get("tool_name")
        or request.get("name")
        or (raw_tool if isinstance(raw_tool, str) else "")
        or request.get("command_name")
        or request.get("action")
        or ""
    )
    if not tool_name and isinstance(raw_tool, dict):
        tool_name = raw_tool.get("tool_name") or raw_tool.get("name") or raw_tool.get("type") or ""
    if not tool_name and isinstance(request.get("tool_call"), dict):
        tool_call = request["tool_call"]
        tool_name = tool_call.get("tool_name") or tool_call.get("name") or tool_call.get("type") or ""
    is_agy = tool_name in AGY_TOOLS
    is_codex = event_name == "PermissionRequest" or tool_name == "apply_patch"

    # PostToolUse Hook Flow
    if event_name == "PostToolUse":
        # Extract content
        output_content = ""
        for k in ["tool_response", "output", "result", "response", "content"]:
            if k in request:
                output_content = str(request[k])
                break
        
        is_error = False
        if "error" in request and request["error"]:
            output_content = str(request["error"])
            is_error = True
            
        tool_id = request.get("tool_use_id", request.get("tool_id", str(request.get("stepIdx", "0"))))
        
        # POST to /api/cc-tool-result
        protocols = ["http", "https"]
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        payload = {
            "loom_conv_id": conv_id,
            "tool_name": normalize_tool_name(tool_name) if is_agy else tool_name,
            "tool_id": tool_id,
            "content": output_content,
            "is_error": is_error
        }
        
        for proto in protocols:
            url = f"{proto}://127.0.0.1:{port}/api/cc-tool-result"
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
                    pass
                break
            except Exception as e:
                sys.stderr.write(f"[Hook] PostToolUse result send failed via {proto}: {e}\n")
        sys.exit(0)

    # PreToolUse Hook Flow
    # Notify Loom that tool execution has started (useful for read-only tools or tracking)
    tool_id = request.get("tool_use_id", request.get("tool_id", str(request.get("stepIdx", "0"))))
    tool_input = (
        request.get("tool_input")
        or request.get("input")
        or request.get("arguments")
        or request.get("args")
        or request.get("params")
        or {}
    )
    if not tool_input and isinstance(request.get("tool_call"), dict):
        tool_call = request["tool_call"]
        tool_input = (
            tool_call.get("tool_input")
            or tool_call.get("input")
            or tool_call.get("arguments")
            or tool_call.get("args")
            or {}
        )
    if not tool_input and isinstance(raw_tool, dict):
        tool_input = (
            raw_tool.get("tool_input")
            or raw_tool.get("input")
            or raw_tool.get("arguments")
            or raw_tool.get("args")
            or {}
        )
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except json.JSONDecodeError:
            tool_input = {"input": tool_input}
    if not tool_input and request.get("command"):
        tool_input = {"command": request.get("command")}
    
    mapped_name = normalize_tool_name(tool_name) if (is_agy or is_codex) else tool_name
    mapped_input = normalize_tool_args(tool_name, tool_input) if (is_agy or is_codex) else tool_input
    
    protocols = ["http", "https"]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    start_payload = {
        "loom_conv_id": conv_id,
        "tool_name": mapped_name,
        "tool_id": tool_id,
        "tool_input": mapped_input
    }
    
    for proto in protocols:
        url = f"{proto}://127.0.0.1:{port}/api/cc-tool-start"
        data = json.dumps(start_payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=3, context=ctx) as resp:
                pass
            break
        except Exception as e:
            sys.stderr.write(f"[Hook] PreToolUse start send failed via {proto}: {e}\n")

    # BACKSTAGE LOCKDOWN:
    # If this is a backstage session, we strictly deny file-writing and shell tools.
    # This forces the agent to use the 'loom-state-cards' MCP server instead.
    if backstage_parent:
        deny_list = {
            "Write", "Edit", "NotebookEdit", "Bash", "Replace"
        }
        if mapped_name in deny_list:
            deny(f"Tool {mapped_name} is disabled in Backstage mode. Use the 'loom-state-cards' MCP tools to manage character and scene data.", event_name=event_name)

    # NROL OPERATOR LOCKDOWN:
    # Same deny set as backstage — the CLI already strips these via
    # --disallowedTools; this is defense in depth if launch args drift.
    if nrol_operator:
        deny_list = {"Write", "Edit", "NotebookEdit", "Bash", "Replace"}
        if mapped_name in deny_list:
            deny(
                f"Tool {mapped_name} is disabled in NROL operator mode. "
                "Use the nrol-ao MCP tools (typed transitions) instead.",
                event_name=event_name,
            )

    # DENY IMAGE READS:
    # If the tool is reading a file, and the path ends with an image extension, we block it for Llama sessions.
    if os.environ.get("LOOM_USE_LLAMA") == "1" and mapped_name in ("Read", "read_file", "view_file", "read_text_file", "read_many_files"):
        fp = (
            mapped_input.get("file_path")
            or mapped_input.get("filePath")
            or tool_input.get("file_path")
            or tool_input.get("filePath")
            or tool_input.get("AbsolutePath")
            or tool_input.get("TargetFile")
        )
        paths_to_check = []
        if isinstance(fp, str):
            paths_to_check.append(fp)
        elif isinstance(fp, list):
            for item in fp:
                if isinstance(item, str):
                    paths_to_check.append(item)
        elif isinstance(fp, dict):
            for v in fp.values():
                if isinstance(v, str):
                    paths_to_check.append(v)

        for p in paths_to_check:
            p_lower = p.lower()
            if any(p_lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff")):
                deny(
                    f"Reading binary image file '{p}' directly is not allowed. "
                    "Use Loom's system-provided image descriptions (which are automatically generated "
                    "when images are attached or processed) or ask the user for details about the image content.",
                    event_name=event_name,
                )

    # Auto-approve read-only tools
    if event_name != "PermissionRequest" and mapped_name in READ_ONLY:
        allow(f"Read-only tool: {mapped_name}", event_name=event_name)

    # Auto-approve the nrol-ao MCP surface entirely. Its read tools are
    # side-effect-free, and its mutating tools (commit=true) raise their own
    # Loom permission request from inside the MCP server — with transition,
    # indicator, and evidence context. Prompting here too would double-prompt
    # every commit with a less informative dialog.
    if event_name != "PermissionRequest" and mapped_name.startswith("mcp__nrol-ao__"):
        allow(f"nrol-ao MCP tool (commits gated server-side): {mapped_name}", event_name=event_name)

    request["loom_conv_id"] = conv_id
    request["tool_name"] = mapped_name # Normalize for Loom API
    request["tool_input"] = mapped_input

    # Try HTTP first, then HTTPS as a fallback
    errors = []
    for proto in protocols:
        url = f"{proto}://127.0.0.1:{port}/api/cc-permission"
        data = json.dumps(request).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"}
        )

        try:
            with urllib.request.urlopen(req, timeout=None, context=ctx) as resp:
                response = json.loads(resp.read().decode("utf-8"))
                # Headless CC never honors ExitPlanMode approval in-session:
                # even after a PreToolUse allow, CC runs its built-in plan
                # prompt, which -p mode cannot render, so it auto-denies with
                # the bare title "Exit plan mode?" (reproduced 2026-06-11 on
                # CC 2.1.173 with an unconditional auto-allow hook). Loom's
                # frontend flips the conversation to Act mode on approval, so
                # the honest in-session signal is an explicit message — not a
                # cryptic error the model reads as a rejection.
                if mapped_name == "ExitPlanMode":
                    if response.get("allow"):
                        deny(
                            "Plan APPROVED by the user in Loom. Headless Claude "
                            "Code cannot exit plan mode mid-session; the next "
                            "turn starts in Act mode with the plan in effect. "
                            "End your turn now — do not retry ExitPlanMode.",
                            event_name=event_name,
                        )
                    deny(
                        "User requested plan revisions in Loom — stay in plan "
                        "mode and refine the plan.",
                        event_name=event_name,
                    )
                if response.get("allow"):
                    allow(f"Approved by user in Loom UI via {proto}", event_name=event_name)
                else:
                    deny(response.get("message", "Denied by user in Loom UI"), event_name=event_name)
                return
        except Exception as e:
            errors.append(f"{proto}: {e}")

    deny(f"Loom server unreachable on {port}. Errors: {', '.join(errors)}", event_name=event_name)


if __name__ == "__main__":
    main()
