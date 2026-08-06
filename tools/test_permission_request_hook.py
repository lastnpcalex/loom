#!/usr/bin/env python3
"""Emit a synthetic Loom permission request and print the resulting hook decision.

Usage:
  C:\\Python314\\python.exe tools\\test_permission_request_hook.py --conv-id 193
  C:\\Python314\\python.exe tools\\test_permission_request_hook.py --conv-id 193 --command "echo permission smoke"

The script intentionally does not execute the command. It only exercises the
same browser approval bridge used by cc_permission_hook.py.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from uuid import uuid4


def _post_json(url: str, payload: dict, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def _post_loom(port: str, path: str, payload: dict, timeout):
    errors = []
    for proto in ("http", "https"):
        url = f"{proto}://127.0.0.1:{port}{path}"
        try:
            return _post_json(url, payload, timeout=timeout), url
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("; ".join(errors))


def _hook_decision(response: dict) -> dict:
    allowed = bool(response.get("allow"))
    if allowed:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": response.get("message") or "Denied by user in Loom UI",
            },
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a synthetic Loom permission request for UI testing."
    )
    parser.add_argument(
        "--conv-id",
        default=os.environ.get("LOOM_CONV_ID", ""),
        help="Loom conversation id. Defaults to LOOM_CONV_ID.",
    )
    parser.add_argument(
        "--port",
        default=os.environ.get("LOOM_PORT", "3000"),
        help="Loom server port. Defaults to LOOM_PORT or 3000.",
    )
    parser.add_argument(
        "--tool",
        default="Bash",
        help="Tool name shown in the permission prompt.",
    )
    parser.add_argument(
        "--command",
        default="echo permission smoke test",
        help="Synthetic command shown in the permission prompt. It is not executed.",
    )
    parser.add_argument(
        "--approval-method",
        default="item/commandExecution/requestApproval",
        help="Optional provider approval method label.",
    )
    parser.add_argument(
        "--scope",
        default="test",
        help="Permission scope sent to Loom. Defaults to 'test'.",
    )
    parser.add_argument(
        "--no-tool-start",
        action="store_true",
        help="Skip the synthetic /api/cc-tool-start event.",
    )
    args = parser.parse_args()

    if not args.conv_id:
        parser.error("--conv-id is required when LOOM_CONV_ID is not set")

    tool_id = f"test-perm-{uuid4().hex[:8]}"
    tool_input = {"command": args.command}

    if not args.no_tool_start:
        start_payload = {
            "loom_conv_id": args.conv_id,
            "tool_name": args.tool,
            "tool_id": tool_id,
            "tool_input": tool_input,
        }
        try:
            _, start_url = _post_loom(args.port, "/api/cc-tool-start", start_payload, timeout=5)
            print(f"[test-perm] sent tool_start via {start_url}", file=sys.stderr)
        except RuntimeError as exc:
            print(f"[test-perm] tool_start failed, continuing: {exc}", file=sys.stderr)

    request_payload = {
        "loom_conv_id": args.conv_id,
        "hook_event_name": "PermissionRequest",
        "tool_name": args.tool,
        "tool_id": tool_id,
        "tool_input": tool_input,
        "approval_method": args.approval_method,
        "permission_scope": args.scope,
    }
    print(
        f"[test-perm] waiting for Loom decision: conv={args.conv_id} "
        f"tool={args.tool} command={args.command!r}",
        file=sys.stderr,
    )
    response, perm_url = _post_loom(
        args.port,
        "/api/cc-permission",
        request_payload,
        timeout=None,
    )
    print(f"[test-perm] decision received via {perm_url}: {response}", file=sys.stderr)
    print(json.dumps(_hook_decision(response)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
