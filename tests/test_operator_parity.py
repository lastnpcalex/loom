"""Multi-provider NROL operator parity (ROADMAP.md "Multi-provider operator parity").

Unit layer only: launch-parameter builders and the provider guard. The live
acceptance checks — sandbox blocks a topic-JSON write without prompting, MCP
commit raises the Loom browser approval, operator recites OPERATOR.md — stay
manual per provider.
"""

import json
from pathlib import Path

import pytest


# --- Step 0: provider guard ------------------------------------------------

def test_operator_guard_allows_claude_family():
    import server

    assert server._nrol_operator_block_reason("sonnet") is None
    assert server._nrol_operator_block_reason("claude-opus-4-6") is None
    # Local llama reuses the claude_client launch profile, so it stays allowed.
    assert server._nrol_operator_block_reason("some-model.gguf") is None


def test_operator_guard_blocks_unported_provider(monkeypatch):
    import server

    monkeypatch.setattr(server, "NROL_OPERATOR_PROVIDERS", {"claude"})
    blocked = server._nrol_operator_block_reason("gpt-5.5")
    assert blocked and "Multi-provider operator parity" in blocked and "'codex'" in blocked
    blocked = server._nrol_operator_block_reason("gemini 3.5 flash")
    assert blocked and "'gemini'" in blocked


async def test_operator_creation_refuses_unported_provider(client, monkeypatch):
    import server

    monkeypatch.setattr(server, "NROL_OPERATOR_PROVIDERS", {"claude"})
    resp = await client.post("/api/conversations", json={
        "title": "operator", "nrol_operator": True, "cc_model": "gpt-5.5",
    })
    assert resp.status_code == 400
    assert "Multi-provider operator parity" in resp.json()["detail"]


async def test_operator_creation_allows_claude(client):
    resp = await client.post("/api/conversations", json={
        "title": "operator", "nrol_operator": True, "cc_model": "sonnet",
    })
    assert resp.status_code == 200


async def test_fork_inherits_operator_flag(tmp_database, tmp_path):
    # Observed live (conv 172): forking an operator conversation dropped
    # nrol_operator, so the fork launched codex with no lockdown — a silent
    # privilege escalation. The flag is identity and must survive the fork.
    import database as db

    conv = await db.create_conversation(
        "Operator", mode="claude", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], nrol_operator=1)
    msg = await db.add_message(conv["id"], "user", "hello")

    fork = await db.fork_conversation(conv["id"], msg["id"])
    forked = await db.get_conversation(fork["id"])
    assert forked["nrol_operator"] == 1


# --- Codex port --------------------------------------------------------------

def test_codex_operator_launch_policies():
    import codex_client

    assert codex_client._codex_launch_policies("default", nrol_operator=True) == (
        "never", "read-only",
    )
    assert codex_client._codex_launch_policies("default") == (
        "on-request", "workspace-write",
    )


def test_codex_operator_sandbox_policy_is_read_only(tmp_path):
    import codex_client

    assert codex_client._app_sandbox_policy(str(tmp_path), nrol_operator=True) == {
        "type": "readOnly",
    }
    default = codex_client._app_sandbox_policy(str(tmp_path))
    assert default["type"] == "workspaceWrite"
    assert default["writableRoots"] == [str(tmp_path)]


def test_codex_operator_thread_mcp_surface_is_strict(tmp_path, monkeypatch):
    import codex_client

    monkeypatch.setenv("NROL_AO_REPO", str(tmp_path))
    servers = codex_client._thread_mcp_servers(7, 8000, nrol_operator=True)
    assert set(servers) == {"nrol-ao", "web-tools"}
    assert servers["nrol-ao"]["env"]["LOOM_CONV_ID"] == "7"
    # Operator threads keep nrol-ao even when auto-registration is off…
    monkeypatch.setenv("NROL_AO_AUTO_MCP", "0")
    assert "nrol-ao" in codex_client._thread_mcp_servers(7, 8000, nrol_operator=True)
    # …while non-operator threads honour the kill-switch and get no web-tools.
    assert codex_client._thread_mcp_servers(7, 8000) == {}


def test_codex_operator_instructions_land_as_agents_md(tmp_path):
    import codex_client

    codex_client._ensure_operator_instructions(tmp_path)
    operator_md = (
        Path(codex_client.__file__).parent / "mcp_servers" / "nrol_ao" / "OPERATOR.md"
    ).read_text(encoding="utf-8")
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == operator_md


async def test_codex_diagnostics_reports_operator_lockdown(tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation(
        "Operator Diag", mode="claude", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], nrol_operator=1)

    result = await server.codex_diagnostics(conv_id=conv["id"])

    assert result["expected_launch"]["sandbox"] == "read-only"
    assert result["expected_launch"]["approval_policy"] == "never"
    assert result["expected_launch"]["writable_roots"] == []
    assert result["expected_launch"]["mcp_servers"] == ["nrol-ao", "web-tools"]


# --- agy port ----------------------------------------------------------------

def test_agy_operator_workspace_config(tmp_path, monkeypatch):
    import codex_client
    import gemini_client

    monkeypatch.setenv("NROL_AO_REPO", str(tmp_path / "engine"))
    (tmp_path / "engine").mkdir()
    # Operator MCP surface must land even when auto-registration is off.
    monkeypatch.setenv("NROL_AO_AUTO_MCP", "0")

    gemini_client._configure_operator(str(tmp_path), conv_id=7, server_port=8123)

    mcp = json.loads(
        (tmp_path / ".agents" / "mcp_config.json").read_text(encoding="utf-8")
    )
    assert set(mcp["mcpServers"]) == {"nrol-ao", "web-tools"}
    assert mcp["mcpServers"]["nrol-ao"]["env"]["LOOM_CONV_ID"] == "7"
    assert mcp["mcpServers"]["nrol-ao"]["env"]["LOOM_PORT"] == "8123"
    # agy's mcp_config.json format carries no codex-only keys.
    assert "default_tools_approval_mode" not in mcp["mcpServers"]["nrol-ao"]

    operator_md = (
        Path(codex_client.__file__).parent / "mcp_servers" / "nrol_ao" / "OPERATOR.md"
    ).read_text(encoding="utf-8")
    assert (tmp_path / "GEMINI.md").read_text(encoding="utf-8") == operator_md


def test_agy_operator_hook_denies_shell_without_prompt(monkeypatch):
    """The hook is agy's only tool-blocking surface — a run_command under
    LOOM_NROL_OPERATOR must deny locally, never reaching the Loom prompt."""
    import io
    import sys as _sys

    import cc_permission_hook

    def fail_urlopen(req, timeout=None, context=None):
        if req.full_url.endswith("/api/cc-permission"):
            raise AssertionError("operator shell deny must not reach the Loom prompt")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{}"

        return _Resp()

    stdin = io.StringIO(json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": "run_command",
        "tool_input": {"command": "echo 0.99 > topic.json"},
    }))
    stdout = io.StringIO()

    monkeypatch.setenv("LOOM_CONV_ID", "42")
    monkeypatch.setenv("LOOM_PORT", "3000")
    monkeypatch.setenv("LOOM_NROL_OPERATOR", "1")
    monkeypatch.setattr(_sys, "argv", ["cc_permission_hook.py", "--event", "PreToolUse"])
    monkeypatch.setattr(_sys, "stdin", stdin)
    monkeypatch.setattr(_sys, "stdout", stdout)
    monkeypatch.setattr(cc_permission_hook.urllib.request, "urlopen", fail_urlopen)

    with pytest.raises(SystemExit):
        cc_permission_hook.main()

    out = json.loads(stdout.getvalue())
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "NROL operator mode" in out["hookSpecificOutput"]["permissionDecisionReason"]
