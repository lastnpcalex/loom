#!/usr/bin/env python3
"""PreToolUse / BeforeTool hook -> Loom HTTP bridge.

Works with both Claude Code (PreToolUse) and Gemini CLI (BeforeTool).
Routes tool permission decisions through Loom's HTTP API so the user
can approve/deny in the browser UI.

Claude Code output format:
  {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow/deny", ...}}

Gemini CLI output format:
  {"decision": "allow/deny", "reason": "..."}

Environment variables (set by Loom when launching CC/Gemini):
  LOOM_PORT: Port of the Loom server (default: 3000)
  LOOM_CONV_ID: Conversation ID in Loom
"""
import sys
import json
import os
import ssl
import urllib.request
import urllib.error


# Tools that don't need user permission (read-only or low-risk)
READ_ONLY = {
    # Claude Code tools (CamelCase)
    "Read", "Glob", "Grep", "WebSearch", "WebFetch", "Task",
    "TaskGet", "TaskList", "TaskUpdate", "TaskCreate", "TaskStop",
    "TaskOutput", "TodoWrite", "Skill",
    "EnterPlanMode", "EnterWorktree", "ExitWorktree",
    "Explore", "CronList", "ToolSearch", "Agent",
    # Gemini CLI tools (snake_case)
    "read_file", "read_many_files", "glob", "grep_search",
    "list_directory", "google_web_search", "web_fetch",
    "write_todos", "enter_plan_mode",
    "cli_help", "codebase_investigator", "get_internal_docs",
}


def allow(reason="Auto-approved", event_name="PreToolUse"):
    """Output allow JSON and exit 0."""
    sys.stderr.write(f"[Hook] Allowing: {reason}\n")
    if event_name == "BeforeTool":
        # Gemini expects a flat object
        output = {
            "decision": "allow",
            "reason": reason
        }
    else:
        # Claude Code expects a wrapper
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
    """Output deny JSON and exit 0."""
    sys.stderr.write(f"[Hook] Denying: {reason}\n")
    if event_name == "BeforeTool":
        # Gemini expects a flat object
        output = {
            "decision": "deny",
            "reason": reason
        }
    else:
        # Claude Code expects a wrapper
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
    sys.stderr.write("[PERM-HOOK] Hook started\n")
    port = os.environ.get("LOOM_PORT", "3000")
    conv_id = os.environ.get("LOOM_CONV_ID", "")
    sys.stderr.write(f"[PERM-HOOK] port={port} conv={conv_id}\n")

    if not conv_id:
        # Not running under Loom — pass through
        sys.exit(0)

    # Read tool info from stdin
    try:
        raw = sys.stdin.read()
        request = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, IOError):
        sys.exit(0)

    # Detect provider from hook_event_name (both Claude and Gemini include this)
    event_name = request.get("hook_event_name", "PreToolUse")
    tool_name = request.get("tool_name", "")

    # Auto-approve read-only tools
    if tool_name in READ_ONLY:
        allow(f"Read-only tool: {tool_name}", event_name=event_name)

    request["loom_conv_id"] = conv_id
    request["tool_name"] = tool_name # Normalize for Loom API

    # Try HTTP first, then HTTPS as a fallback
    protocols = ["http", "https"]
    
    # Skip cert verification for localhost (self-signed)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    errors = []
    for proto in protocols:
        url = f"{proto}://127.0.0.1:{port}/api/cc-permission"
        data = json.dumps(request).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"}
        )

        try:
            with urllib.request.urlopen(req, timeout=600, context=ctx) as resp:
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
