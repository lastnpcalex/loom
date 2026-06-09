import asyncio
import io
import json
import sys

import pytest


def test_codex_default_permission_mode_requests_user_approval():
    import codex_client

    assert codex_client._codex_approval_policy("default") == "on-request"
    assert codex_client._codex_approval_policy("plan") == "on-request"
    assert codex_client._codex_approval_policy(None) == "on-request"
    assert codex_client._codex_approval_policy("on-request") == "on-request"
    assert codex_client._codex_approval_policy("never") == "never"


def test_codex_app_server_approval_mapping_is_session_scoped():
    import codex_client

    assert codex_client._app_approval_response(
        "item/commandExecution/requestApproval",
        allow=True,
        always=False,
    ) == {"decision": "accept"}
    assert codex_client._app_approval_response(
        "item/commandExecution/requestApproval",
        allow=True,
        always=True,
    ) == {"decision": "acceptForSession"}
    assert codex_client._app_approval_response(
        "item/fileChange/requestApproval",
        allow=False,
        always=False,
    ) == {"decision": "decline"}


def test_codex_permissions_approval_echoes_requested_permissions():
    import codex_client

    requested = {
        "filesystem": {
            "entries": [
                {
                    "path": r"C:\repo\.git",
                    "access": "write",
                }
            ]
        },
        "network": {"enabled": False},
    }

    assert codex_client._app_approval_response(
        "item/permissions/requestApproval",
        allow=True,
        always=True,
        params={"permissions": requested},
    ) == {
        "permissions": requested,
        "scope": "session",
    }


def test_codex_app_server_sandbox_policy_scopes_workspace(tmp_path):
    import codex_client

    policy = codex_client._app_sandbox_policy(str(tmp_path))

    assert policy["type"] == "workspaceWrite"
    assert policy["writableRoots"] == [str(tmp_path)]
    assert policy["networkAccess"] is False


async def test_codex_app_server_permission_uses_direct_handler():
    import codex_client

    calls = []

    async def handler(payload):
        calls.append(payload)
        return {"allow": True, "always": True}

    result = await codex_client._post_loom_permission(
        12345,
        77,
        "item/fileChange/requestApproval",
        {"fileChanges": [{"path": "proof.txt"}]},
        permission_scope="gen:12",
        permission_request_handler=handler,
    )

    assert result == {"allow": True, "always": True}
    assert len(calls) == 1
    assert calls[0]["loom_conv_id"] == 77
    assert calls[0]["source"] == "codex_app_server"
    assert calls[0]["approval_method"] == "item/fileChange/requestApproval"
    assert calls[0]["tool_name"] == "Edit"
    assert calls[0]["permission_scope"] == "gen:12"


def test_codex_permission_hook_posts_to_loom_and_outputs_allow(monkeypatch):
    import cc_permission_hook

    calls = []

    class FakeResponse:
        def __init__(self, body=b"{}"):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self._body

    def fake_urlopen(req, timeout=None, context=None):
        body = json.loads(req.data.decode("utf-8"))
        calls.append((req.full_url, body, timeout))
        if req.full_url.endswith("/api/cc-permission"):
            return FakeResponse(json.dumps({"allow": True}).encode("utf-8"))
        return FakeResponse()

    stdin = io.StringIO(json.dumps({
        "hook_event_name": "PermissionRequest",
        "tool": {
            "name": "shell_command",
            "arguments": {"command": "Set-Content proof.txt APPROVED"},
        },
    }))
    stdout = io.StringIO()

    monkeypatch.setenv("LOOM_CONV_ID", "42")
    monkeypatch.setenv("LOOM_PORT", "3000")
    monkeypatch.setattr(sys, "argv", ["cc_permission_hook.py", "--event", "PermissionRequest"])
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(cc_permission_hook.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SystemExit) as exit_info:
        cc_permission_hook.main()

    assert exit_info.value.code == 0
    permission_calls = [c for c in calls if c[0].endswith("/api/cc-permission")]
    assert len(permission_calls) == 1
    sent_payload = permission_calls[0][1]
    assert sent_payload["loom_conv_id"] == "42"
    assert sent_payload["tool_name"] == "shell_command"
    assert sent_payload["tool_input"] == {"command": "Set-Content proof.txt APPROVED"}

    hook_output = json.loads(stdout.getvalue())
    assert hook_output["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert hook_output["hookSpecificOutput"]["decision"]["behavior"] == "allow"


async def test_loom_permission_endpoint_blocks_until_user_decision(tmp_database):
    import database as db
    import server

    server._pending_hook_permissions.clear()
    server._auto_approve_permissions.clear()
    conv = await db.create_conversation(
        "Permission Test",
        mode="codex",
        project_dir=str(server.Path.cwd()),
    )

    request_task = asyncio.create_task(server.handle_cc_permission({
        "loom_conv_id": conv["id"],
        "tool_name": "Bash",
        "tool_input": {"command": "Set-Content proof.txt APPROVED"},
    }))

    for _ in range(50):
        if server._pending_hook_permissions:
            break
        await asyncio.sleep(0.02)

    assert len(server._pending_hook_permissions) == 1
    request_id, pending = next(iter(server._pending_hook_permissions.items()))
    conn = await db.get_db()
    rows = await conn.execute_fetchall(
        "SELECT conv_id, tool_name, input_summary FROM pending_permissions WHERE request_id = ?",
        (request_id,),
    )
    assert len(rows) == 1
    assert rows[0]["conv_id"] == conv["id"]
    assert rows[0]["tool_name"] == "Bash"
    assert json.loads(rows[0]["input_summary"]) == "Set-Content proof.txt APPROVED"

    try:
        assert not request_task.done()
        pending["response"] = {"allow": False}
        pending["event"].set()

        response = await asyncio.wait_for(request_task, timeout=1)
        assert response == {"allow": False, "message": "Denied by user in Loom UI"}
        assert request_id not in server._pending_hook_permissions
    finally:
        if not request_task.done():
            request_task.cancel()


def test_loom_permission_fingerprint_scopes_remembered_grants():
    import server

    assert server._permission_fingerprint(
        "Bash",
        {"command": "git status"},
    ) == "Bash"
    assert server._permission_fingerprint(
        "Edit",
        {"file_path": "proof.txt"},
    ) == "Edit"
    assert server._permission_fingerprint(
        "Bash",
        {"command": "git status"},
        "item/commandExecution/requestApproval",
    ) == "item/commandExecution/requestApproval:git status"
    assert server._permission_fingerprint(
        "Edit",
        {"fileChanges": [{"path": "proof.txt"}]},
        "item/fileChange/requestApproval",
    ) == 'item/fileChange/requestApproval:{"fileChanges":[{"path":"proof.txt"}]}'
    assert server._permission_fingerprint(
        "Edit",
        {"fileChanges": [{"path": "other.txt"}]},
        "item/fileChange/requestApproval",
    ) != server._permission_fingerprint(
        "Edit",
        {"fileChanges": [{"path": "proof.txt"}]},
        "item/fileChange/requestApproval",
    )

    requested_a = {"filesystem": {"entries": [{"path": "repo", "access": "write"}]}}
    requested_b = {"filesystem": {"entries": [{"path": "repo/.git", "access": "write"}]}}
    assert server._permission_fingerprint(
        "PermissionRequest",
        {"permissions": requested_a},
        "item/permissions/requestApproval",
    ) != server._permission_fingerprint(
        "PermissionRequest",
        {"permissions": requested_b},
        "item/permissions/requestApproval",
    )


async def test_codex_diagnostics_reports_target_folder(tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation(
        "Diagnostics Test",
        mode="codex",
        project_dir=str(tmp_path),
    )

    result = await server.codex_diagnostics(conv_id=conv["id"])

    assert result["target_dir"] == str(tmp_path.resolve())
    assert result["expected_launch"]["approval_policy"] == "on-request"
    assert result["expected_launch"]["surface"] == "app-server"
    assert result["expected_launch"]["hook_scope"] == "disabled"
    assert result["expected_launch"]["sandbox"] == "workspace-write"
    assert result["expected_launch"]["writable_roots"] == [str(tmp_path.resolve())]
    assert result["project_hook"]["ignored_by_loom_codex"] is True
    assert result["project_hook"]["path"].endswith(".codex\\hooks.json") or result["project_hook"]["path"].endswith(".codex/hooks.json")


def test_codex_diff_events_normalize_to_edit_tool_payload():
    import codex_client

    raw = {
        "method": "turn/diff/updated",
        "params": {
            "threadId": "thr",
            "turnId": "turn",
            "diff": "diff --git a/a.txt b/a.txt\n+hello",
        },
    }

    assert codex_client._codex_diff_tool_id(raw) == "codex-diff:turn"
    payload = codex_client._codex_diff_payload(raw)
    assert payload["kind"] == "codex_diff"
    assert payload["threadId"] == "thr"
    assert payload["turnId"] == "turn"
    assert payload["diff"].startswith("diff --git")


def test_permission_scope_gen_id_parses_generation_scope():
    import server

    assert server._permission_scope_gen_id("gen:42") == 42
    assert server._permission_scope_gen_id("manual") is None
