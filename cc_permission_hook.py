#!/usr/bin/env python3
"""PreToolUse / BeforeTool hook -> Loom HTTP bridge.

Works with both Claude Code (PreToolUse) and Antigravity/agy (PreToolUse).
Routes tool permission decisions through Loom's HTTP API so the user
can approve/deny in the browser UI.

Claude Code output format:
  {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow/deny", ...}}

Antigravity (agy) output format:
  {"decision": "allow/deny", "reason": "..."}

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


def allow(reason="Auto-approved", event_name="PreToolUse"):
    """Output allow JSON and exit 0."""
    sys.stderr.write(f"[Hook] Allowing: {reason}\n")
    if event_name == "BeforeTool":
        output = {
            "decision": "allow",
            "reason": reason
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
    elif len(sys.argv) > 1 and sys.argv[1] in ("PreToolUse", "PostToolUse", "BeforeTool"):
        event_name = sys.argv[1]

    sys.stderr.write(f"[PERM-HOOK] Hook started for event {event_name}\n")
    port = os.environ.get("LOOM_PORT", "3000")
    conv_id = os.environ.get("LOOM_CONV_ID", "")
    backstage_parent = os.environ.get("LOOM_BACKSTAGE_PARENT_ID", "")
    sys.stderr.write(f"[PERM-HOOK] port={port} conv={conv_id} backstage={backstage_parent}\n")

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

    tool_name = request.get("tool_name", "")

    # PostToolUse Hook Flow
    if event_name == "PostToolUse":
        # Extract content
        output_content = ""
        for k in ["output", "result", "response", "content"]:
            if k in request:
                output_content = str(request[k])
                break
        
        is_error = False
        if "error" in request and request["error"]:
            output_content = str(request["error"])
            is_error = True
            
        tool_id = request.get("tool_id", str(request.get("stepIdx", "0")))
        
        # POST to /api/cc-tool-result
        protocols = ["http", "https"]
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        payload = {
            "loom_conv_id": conv_id,
            "tool_name": tool_name,
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
    tool_id = request.get("tool_id", str(request.get("stepIdx", "0")))
    tool_input = request.get("tool_input", {})
    
    protocols = ["http", "https"]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    start_payload = {
        "loom_conv_id": conv_id,
        "tool_name": tool_name,
        "tool_id": tool_id,
        "tool_input": tool_input
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
            "Write", "Edit", "NotebookEdit", "Bash", "Replace", "write_file", "run_shell_command", "replace"
        }
        if tool_name in deny_list:
            deny(f"Tool {tool_name} is disabled in Backstage mode. Use the 'loom-state-cards' MCP tools to manage character and scene data.", event_name=event_name)

    # Auto-approve read-only tools
    if tool_name in READ_ONLY:
        allow(f"Read-only tool: {tool_name}", event_name=event_name)

    request["loom_conv_id"] = conv_id
    request["tool_name"] = tool_name # Normalize for Loom API

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
