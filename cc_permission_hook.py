#!/usr/bin/env python3
"""CC PreToolUse hook -> Loom HTTP bridge.

Invoked by Claude Code before each tool execution. Routes the
permission decision through Loom's HTTP API so the user can
approve/deny in the browser UI.

For PreToolUse hooks:
  - Exit 0 + JSON with permissionDecision "allow" = approve the tool
  - Exit 0 + JSON with permissionDecision "deny"  = block the tool
  - Exit 0 + no output = pass through (normal permission flow — denies in -p mode)
  - Exit 2 = ignore hook output, revert to default behavior

Environment variables (set by Loom when launching CC):
  LOOM_PORT: Port of the Loom server (default: 8000)
  LOOM_CONV_ID: Conversation ID in Loom
"""
import sys
import json
import os
import ssl
import urllib.request
import urllib.error


# Read-only tools that don't need user permission
READ_ONLY = {"Read", "Glob", "Grep", "WebSearch", "WebFetch", "Task",
             "TaskGet", "TaskList", "TaskUpdate", "AskUserQuestion",
             "EnterPlanMode", "ExitPlanMode", "Explore"}


def allow(reason="Auto-approved"):
    """Output allow JSON and exit 0."""
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
    }))
    sys.stdout.flush()
    sys.exit(0)


def deny(reason="Blocked"):
    """Output deny JSON and exit 0."""
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.stdout.flush()
    sys.exit(0)


def main():
    port = os.environ.get("LOOM_PORT", "3000")
    conv_id = os.environ.get("LOOM_CONV_ID", "")

    if not conv_id:
        # Not running under Loom — pass through
        sys.exit(0)

    # Read tool info from stdin
    try:
        raw = sys.stdin.read()
        request = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, IOError):
        sys.exit(0)

    tool_name = request.get("tool_name", "")

    # Auto-approve read-only tools
    if tool_name in READ_ONLY:
        allow("Read-only tool")

    request["loom_conv_id"] = conv_id

    # POST to Loom server — blocks until user responds (up to 5 min)
    url = f"https://127.0.0.1:{port}/api/cc-permission"
    data = json.dumps(request).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"}
    )

    # Skip cert verification for localhost (self-signed)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, timeout=300, context=ctx) as resp:
            response = json.loads(resp.read().decode("utf-8"))
            if response.get("allow"):
                allow("Approved by user in Loom UI")
            else:
                deny(response.get("message", "Denied by user in Loom UI"))
    except urllib.error.URLError as e:
        deny(f"Loom unreachable: {e}")
    except Exception as e:
        deny(f"Hook error: {e}")


if __name__ == "__main__":
    main()
