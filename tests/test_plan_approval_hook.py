"""ExitPlanMode through the permission hook.

Headless CC cannot exit plan mode in-session: even after a PreToolUse allow,
CC's built-in plan prompt cannot render in -p mode and auto-denies with the
bare title "Exit plan mode?" (reproduced 2026-06-11, CC 2.1.173, with an
unconditional auto-allow hook). The hook therefore translates the user's
Loom decision into an explicit message instead of passing allow through —
approval reads as approval, not as a cryptic error.
"""

import io
import json
import sys

import pytest


class _FakeResponse:
    def __init__(self, body=b"{}"):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _run_hook(monkeypatch, tool_name, loom_allows):
    import cc_permission_hook

    def fake_urlopen(req, timeout=None, context=None):
        if req.full_url.endswith("/api/cc-permission"):
            return _FakeResponse(json.dumps({"allow": loom_allows}).encode("utf-8"))
        return _FakeResponse()

    stdin = io.StringIO(json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"plan": "test plan"},
    }))
    stdout = io.StringIO()

    monkeypatch.setenv("LOOM_CONV_ID", "42")
    monkeypatch.setenv("LOOM_PORT", "3000")
    monkeypatch.delenv("LOOM_NROL_OPERATOR", raising=False)
    monkeypatch.delenv("LOOM_BACKSTAGE_PARENT_ID", raising=False)
    monkeypatch.setattr(sys, "argv", ["cc_permission_hook.py", "--event", "PreToolUse"])
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(cc_permission_hook.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SystemExit):
        cc_permission_hook.main()

    return json.loads(stdout.getvalue())["hookSpecificOutput"]


def test_plan_approval_reads_as_approval_not_cryptic_error(monkeypatch):
    out = _run_hook(monkeypatch, "ExitPlanMode", loom_allows=True)
    # Deny at the hook level (CC would auto-deny anyway), but with a message
    # the model reads as approval + next-turn instructions.
    assert out["permissionDecision"] == "deny"
    assert "APPROVED" in out["permissionDecisionReason"]
    assert "do not retry ExitPlanMode" in out["permissionDecisionReason"]


def test_plan_revision_request_says_stay_in_plan_mode(monkeypatch):
    out = _run_hook(monkeypatch, "ExitPlanMode", loom_allows=False)
    assert out["permissionDecision"] == "deny"
    assert "revisions" in out["permissionDecisionReason"]
    assert "stay in plan" in out["permissionDecisionReason"]


def test_non_plan_tools_still_pass_allow_through(monkeypatch):
    out = _run_hook(monkeypatch, "Write", loom_allows=True)
    assert out["permissionDecision"] == "allow"
