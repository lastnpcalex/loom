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


def test_codex_model_mapping_preserves_explicit_latest_selection(monkeypatch):
    import codex_client

    monkeypatch.delenv("LOOM_CODEX_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)

    assert codex_client._loom_model_to_codex("codex-gpt-5.6-sol") == "gpt-5.6-sol"
    assert codex_client._loom_model_to_codex("Codex (GPT-5.6-Sol)") == "gpt-5.6-sol"
    assert codex_client._loom_model_to_codex("gpt-5.6-terra") == "gpt-5.6-terra"
    assert codex_client._loom_model_to_codex("codex-my-explicit-model") == "my-explicit-model"


def test_codex_model_mapping_uses_cache_only_when_selection_missing(monkeypatch, tmp_path):
    import codex_client

    home = tmp_path / "home"
    cache_dir = home / ".codex"
    cache_dir.mkdir(parents=True)
    (cache_dir / "models_cache.json").write_text(
        json.dumps({
            "models": [
                {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol", "supported_in_api": True},
                {"slug": "gpt-5.5", "display_name": "GPT-5.5", "supported_in_api": True},
            ]
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("LOOM_CODEX_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)

    assert codex_client._loom_model_to_codex("") == "gpt-5.6-sol"
    assert codex_client._loom_model_to_codex("codex-new-provider-model") == "new-provider-model"


def test_codex_reasoning_effort_preserves_current_levels():
    import codex_client

    assert codex_client._codex_reasoning_effort("xhigh") == "xhigh"
    assert codex_client._codex_reasoning_effort("max") == "max"
    assert codex_client._codex_reasoning_effort("ultra") == "ultra"
    assert codex_client._codex_reasoning_effort("minimal") == "low"
    assert codex_client._codex_reasoning_effort("invalid") is None


def test_codex_thread_request_disables_silent_model_fallback():
    import codex_client

    method, params = codex_client._codex_thread_request(
        "C:/workspace",
        "gpt-5.6-sol",
        "on-request",
        "workspace-write",
    )

    assert method == "thread/start"
    assert params["model"] == "gpt-5.6-sol"
    assert params["allowProviderModelFallback"] is False


def test_codex_model_attestation_separates_ui_request_from_effective_model():
    import codex_client

    attestation = codex_client._codex_model_attestation(
        "codex-gpt-5.6-sol",
        "gpt-5.6-sol",
        {
            "model": "gpt-5.6-sol",
            "modelProvider": "openai",
            "thread": {"id": "thread-123"},
        },
        session_id="thread-123",
        app_server_user_agent="codex_cli_rs/1.2.3",
    )

    assert attestation == {
        "status": "verified",
        "harness": "Codex app-server",
        "requested_model": "codex-gpt-5.6-sol",
        "launch_model": "gpt-5.6-sol",
        "effective_model": "gpt-5.6-sol",
        "model_provider": "openai",
        "source": "codex_app_server_thread_response",
        "verification_level": "harness",
        "session_id": "thread-123",
        "thread_id": "thread-123",
        "app_server": "codex_cli_rs/1.2.3",
        "fallback_allowed": False,
    }


def test_codex_model_attestation_detects_mismatch_and_missing_evidence():
    import codex_client

    mismatch = codex_client._codex_model_attestation(
        "codex-gpt-5.6-sol",
        "gpt-5.6-sol",
        {"model": "gpt-5.6-terra", "modelProvider": "openai"},
    )
    missing = codex_client._codex_model_attestation(
        "codex-gpt-5.6-sol",
        "gpt-5.6-sol",
        {"model": "gpt-5.6-sol"},
    )

    assert mismatch["status"] == "mismatch"
    assert mismatch["effective_model"] == "gpt-5.6-terra"
    assert missing["status"] == "unverified"
    assert missing["effective_model"] == "gpt-5.6-sol"
    assert missing["model_provider"] is None


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


def test_codex_goal_set_params_use_app_server_shape():
    import codex_client

    params = codex_client._codex_goal_set_params(
        "thr_123",
        objective="Finish the migration",
        status="active",
        token_budget=40000,
    )

    assert params == {
        "threadId": "thr_123",
        "objective": "Finish the migration",
        "status": "active",
        "tokenBudget": 40000,
    }


def test_codex_slash_commands_include_goal():
    import skill_scanner

    commands = skill_scanner.get_all_skills(agent="codex")
    goal = next(cmd for cmd in commands if cmd["name"] == "goal")

    assert goal["command"] == "/goal"
    assert goal["mode"] == "meta"


def test_codex_token_usage_updated_event_is_normalized():
    import codex_client

    raw = {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": "thr_1",
            "tokenUsage": {
                "totalInputTokens": 1234,
                "totalOutputTokens": 567,
            },
        },
    }

    assert codex_client._codex_usage(raw) == {
        "type": "usage",
        "input_tokens": 1234,
        "output_tokens": 567,
    }


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
    monkeypatch.setenv("LOOM_PERMISSION_SCOPE", "gen:314")
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
    assert sent_payload["permission_scope"] == "gen:314"
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


def test_permission_command_guard_blocks_nested_goose_cmd_delete():
    import server

    tool_input = {
        "toolCall": {
            "title": "shell",
            "arguments": {
                "command": 'cmd /c rmdir /s /q "C:\\Users\\exast\\OneDrive\\Documents\\Design-Language"',
            },
        },
        "params": {
            "toolCall": {
                "kind": "execute",
                "name": "shell",
            },
        },
    }

    reason = server._unsafe_shell_command_reason("shell", tool_input)

    assert "recursive rmdir" in reason


def test_permission_command_guard_allows_non_destructive_windows_probe():
    import server

    reason = server._unsafe_shell_command_reason(
        "Bash",
        {"command": 'cd "C:/Users/exast/OneDrive/Documents/Design-Language" && git status --short'},
    )

    assert reason == ""


def test_destructive_git_is_approval_gated_instead_of_blocked():
    import server

    tool_input = {"command": "git reset --hard HEAD"}
    assert server._unsafe_shell_command_reason("Bash", tool_input) == ""
    assert "discards tracked workspace changes" in server._destructive_git_command_reason(
        "Bash", tool_input
    )


@pytest.mark.parametrize(
    "command, reason_fragment",
    [
        ("git clean -fd", "untracked workspace files"),
        ("git checkout -f main", "discard workspace changes"),
        ("git switch --discard-changes main", "discard workspace changes"),
        ("git push origin main --force-with-lease", "remote branch history"),
        ("git push origin --delete obsolete", "deleting a remote Git branch"),
    ],
)
def test_destructive_git_variants_require_loom_approval(command, reason_fragment):
    import server

    assert reason_fragment in server._destructive_git_command_reason(
        "Bash", {"command": command}
    )


async def test_destructive_git_permission_waits_for_one_time_user_decision(tmp_database):
    import database as db
    import server

    server._pending_hook_permissions.clear()
    server._auto_approve_permissions.clear()
    conv = await db.create_conversation(
        "Destructive Git Approval",
        mode="codex",
        project_dir=str(server.Path.cwd()),
    )
    tool_input = {"command": "git restore -- static/chat.js"}
    fingerprint = server._permission_fingerprint("Bash", tool_input)
    server._auto_approve_permissions[(conv["id"], "manual")] = {fingerprint}

    request_task = asyncio.create_task(
        server.handle_cc_permission(
            {
                "loom_conv_id": conv["id"],
                "tool_name": "Bash",
                "tool_input": tool_input,
            }
        )
    )
    for _ in range(50):
        if server._pending_hook_permissions:
            break
        await asyncio.sleep(0.02)

    assert len(server._pending_hook_permissions) == 1
    request_id, pending = next(iter(server._pending_hook_permissions.items()))
    assert pending["risk_level"] == "destructive"
    assert pending["supports_allow_all"] is False
    assert "overwrites current workspace" in pending["risk_reason"]
    try:
        pending["response"] = {"allow": False, "always": False}
        pending["event"].set()
        response = await asyncio.wait_for(request_task, timeout=1)
        assert response["allow"] is False
    finally:
        server._pending_hook_permissions.pop(request_id, None)
        server._auto_approve_permissions.clear()
        if not request_task.done():
            request_task.cancel()


def test_permission_command_guard_extracts_goose_json_arguments():
    import server

    reason = server._unsafe_shell_command_reason(
        "shell",
        {
            "toolCall": {
                "name": "shell",
                "arguments": '{"command": "cmd /c del /s /q C:\\\\tmp\\\\bad"}',
            }
        },
    )

    assert "recursive del" in reason


def test_permission_command_guard_allows_native_goose_tree_tool():
    import server

    reason = server._unsafe_shell_command_reason(
        "tree · C:/Users/exast/OneDrive/Documents/Design-Language",
        {
            "toolCall": {
                "title": "tree",
                "arguments": {"path": "C:/Users/exast/OneDrive/Documents/Design-Language"},
            }
        },
    )

    assert reason == ""
    assert server._goose_auto_allow_reason(
        "tree · C:/Users/exast/OneDrive/Documents/Design-Language",
        {
            "toolCall": {
                "title": "tree",
                "arguments": {"path": "C:/Users/exast/OneDrive/Documents/Design-Language"},
            }
        },
    ) == "Goose native tree tool"


def test_permission_command_guard_blocks_unbounded_shell_scans():
    import server

    assert "tree scan" in server._unsafe_shell_command_reason(
        "shell",
        {"command": "cmd /c tree C:\\Users\\exast\\OneDrive\\Documents\\Design-Language /f"},
    )
    assert "recursive directory scan" in server._unsafe_shell_command_reason(
        "shell",
        {"command": 'powershell -NoProfile -Command "Get-ChildItem -Recurse C:\\Users\\exast"'},
    )


async def test_loom_permission_endpoint_auto_denies_unsafe_shell_command(tmp_database):
    import database as db
    import server

    server._pending_hook_permissions.clear()
    server._auto_approve_permissions.clear()
    conv = await db.create_conversation(
        "Unsafe Permission Test",
        mode="goose",
        project_dir=str(server.Path.cwd()),
    )

    response = await server.handle_cc_permission({
        "loom_conv_id": conv["id"],
        "tool_name": "shell",
        "tool_input": {
            "toolCall": {
                "title": "shell",
                "arguments": {
                    "command": "powershell -NoProfile -Command \"Remove-Item -Recurse -Force .\"",
                },
            },
        },
    })

    assert response["allow"] is False
    assert "Blocked by Loom command safeguard" in response["message"]
    assert not server._pending_hook_permissions

    conn = await db.get_db()
    rows = await conn.execute_fetchall(
        "SELECT request_id FROM pending_permissions WHERE conv_id = ?",
        (conv["id"],),
    )
    assert rows == []


async def test_loom_permission_endpoint_auto_allows_native_goose_tree(tmp_database):
    import database as db
    import server

    server._pending_hook_permissions.clear()
    server._auto_approve_permissions.clear()
    conv = await db.create_conversation(
        "Safe Goose Permission Test",
        mode="goose",
        project_dir=str(server.Path.cwd()),
    )

    response = await server.handle_cc_permission({
        "loom_conv_id": conv["id"],
        "tool_name": "tree · C:/Users/exast/OneDrive/Documents/Design-Language",
        "tool_input": {
            "toolCall": {
                "title": "tree",
                "arguments": {"path": "C:/Users/exast/OneDrive/Documents/Design-Language"},
            },
            "params": {
                "toolCall": {
                    "title": "tree",
                    "arguments": {"path": "C:/Users/exast/OneDrive/Documents/Design-Language"},
                },
            },
        },
    })

    assert response["allow"] is True
    assert "Goose native tree tool" in response["message"]
    assert not server._pending_hook_permissions

    conn = await db.get_db()
    rows = await conn.execute_fetchall(
        "SELECT request_id FROM pending_permissions WHERE conv_id = ?",
        (conv["id"],),
    )
    assert rows == []


def test_goose_auto_allow_detects_todo_bookkeeping():
    import server

    reason = server._goose_auto_allow_reason(
        "Write",
        {
            "toolCall": {
                "title": "Write",
                "arguments": {
                    "path": ".goose/todos.json",
                    "content": "{\"todos\": []}",
                },
            },
            "params": {"toolCall": {"title": "Write"}},
        },
    )

    assert reason == "Goose todo bookkeeping"


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


def test_codex_app_server_status_notifications_can_finalize_turns():
    import codex_client

    completed = {
        "method": "thread/status/changed",
        "params": {"threadId": "thr", "status": "idle"},
    }
    failed = {
        "method": "turn/status/changed",
        "params": {
            "turn": {"id": "turn-1", "status": "failed"},
            "error": {"message": "boom"},
        },
    }

    assert codex_client._codex_terminal_status(completed) == (True, False, "")
    assert codex_client._codex_turn_id(failed) == "turn-1"
    assert codex_client._codex_terminal_status(failed) == (True, True, "boom")


def test_codex_status_value_unwraps_object_status_payload():
    import codex_client

    nested_idle = {
        "method": "thread/status/changed",
        "params": {"thread": {"id": "thr", "status": {"type": "idle"}}},
    }
    assert codex_client._codex_status_value(nested_idle) == "idle"
    assert codex_client._codex_terminal_status(nested_idle) == (True, False, "")

    nested_failed = {
        "method": "turn/status/changed",
        "params": {
            "turn": {"id": "turn-1", "status": {"type": "failed"}},
            "error": {"message": "boom"},
        },
    }
    assert codex_client._codex_status_value(nested_failed) == "failed"
    assert codex_client._codex_terminal_status(nested_failed) == (True, True, "boom")


def test_permission_scope_gen_id_parses_generation_scope():
    import server

    assert server._permission_scope_gen_id("gen:42") == 42
    assert server._permission_scope_gen_id("manual") is None


def test_test_permission_request_hook_decision_output():
    from tools import test_permission_request_hook

    allow_output = test_permission_request_hook._hook_decision({"allow": True})
    assert allow_output["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert allow_output["hookSpecificOutput"]["decision"]["behavior"] == "allow"

    deny_output = test_permission_request_hook._hook_decision({
        "allow": False,
        "message": "nope",
    })
    assert deny_output["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert deny_output["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert deny_output["hookSpecificOutput"]["decision"]["message"] == "nope"


def test_permission_hook_denies_image_reads(monkeypatch):
    import cc_permission_hook

    stdin = io.StringIO(json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"filePath": "some/path/test_image.png"},
    }))
    stdout = io.StringIO()

    monkeypatch.setenv("LOOM_CONV_ID", "42")
    monkeypatch.setenv("LOOM_PORT", "3000")
    monkeypatch.setenv("LOOM_USE_LLAMA", "1")
    monkeypatch.setattr(sys, "argv", ["cc_permission_hook.py", "PreToolUse"])
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as exit_info:
        cc_permission_hook.main()

    # The permission hook should exit with sys.exit(0) but deny output
    assert exit_info.value.code == 0
    hook_output = json.loads(stdout.getvalue())
    assert hook_output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Reading binary image file" in hook_output["hookSpecificOutput"]["permissionDecisionReason"]
