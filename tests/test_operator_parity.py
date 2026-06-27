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


def test_operator_guard_allows_umans():
    """umans launches through claude_client (same lockdown: --strict-mcp-config,
    OPERATOR.md, LOOM_NROL_OPERATOR, tool stripping) so it is a ported operator
    provider. Tracked as its own allowlist entry so the parity matrix stays an
    explicit signpost — see mcp_servers/nrol_ao/ROADMAP.md. Regression for the
    live block: 'NROL operator mode is not ported to provider umans yet'."""
    import server

    assert server._nrol_operator_block_reason("umans-glm-5.2") is None
    assert server._nrol_operator_block_reason("umans-coder") is None
    assert server._nrol_operator_block_reason("umans-flash") is None
    # And it is classified as its own provider string (the signpost), not
    # silently folded into "claude".
    assert "umans" in server.NROL_OPERATOR_PROVIDERS


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
    monkeypatch.setenv("NROL_AO_STATE_DIR", str(tmp_path / "state"))
    servers = codex_client._thread_mcp_servers(7, 8000, nrol_operator=True)
    assert set(servers) == {"nrol-ao", "web-tools"}
    assert servers["nrol-ao"]["env"]["LOOM_CONV_ID"] == "7"
    assert servers["nrol-ao"]["env"]["NROL_AO_STATE_DIR"] == str(tmp_path / "state")
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
    monkeypatch.setenv("NROL_AO_STATE_DIR", str(tmp_path / "state"))
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
    assert mcp["mcpServers"]["nrol-ao"]["env"]["NROL_AO_STATE_DIR"] == str(tmp_path / "state")
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


# --- Resume and Fork tests --------------------------------------------------

@pytest.mark.asyncio
async def test_gemini_client_resume_and_fork_args(monkeypatch, tmp_path):
    import gemini_client
    import asyncio

    args_captured = []

    class MockProcess:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.returncode = 0
        async def wait(self):
            return 0

    mock_proc = MockProcess()
    # Feed dummy data to stdin/stdout so it initializes successfully and exits
    mock_proc.stdout.feed_data(b'{"type":"session_info"}\n')
    mock_proc.stdout.feed_eof()
    mock_proc.stderr.feed_eof()

    async def mock_create_subprocess_exec(*args, **kwargs):
        args_captured.append(args)
        return mock_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create_subprocess_exec)
    monkeypatch.setattr(gemini_client, "_find_agy_exe", lambda: "agy")

    # Override HOME / USERPROFILE env variables to direct brain path to tmp_path
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    brain_path = tmp_path / ".gemini" / "antigravity-cli" / "brain"
    conversations_path = tmp_path / ".gemini" / "antigravity-cli" / "conversations"
    conversations_path.mkdir(parents=True, exist_ok=True)

    # Create dummy source session
    src_session = brain_path / "src_session"
    logs_dir = src_session / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "transcript.jsonl").write_text('{"type":"step"}\n', encoding="utf-8")

    # Create dummy source SQLite DB
    import sqlite3
    db_src_path = conversations_path / "src_session.db"
    conn = sqlite3.connect(db_src_path)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT)")
    cursor.execute("INSERT INTO trajectory_meta VALUES ('traj-123', 'src_session')")
    conn.commit()
    conn.close()

    # Create dummy source Protobuf file
    pb_src_path = conversations_path / "src_session.pb"
    pb_src_path.write_bytes(b"some prefix src_session suffix")

    # Run with resume and fork
    await gemini_client.run_gemini(
        prompt="hello",
        cwd=str(tmp_path),
        conv_id=123,
        resume_session_id="src_session",
        fork_session=True,
    )

    # Check the CLI args carried the destination session ID
    assert len(args_captured) == 1
    cmd_args = args_captured[0]
    assert cmd_args[0] == "agy"
    idx = cmd_args.index("--conversation")
    dst_session_id = cmd_args[idx + 1]
    assert dst_session_id != "src_session"
    assert dst_session_id != "123"

    # Check that it forked (copied) the directory to the new destination session ID
    dst_dir = brain_path / dst_session_id
    assert dst_dir.exists()
    assert (dst_dir / ".system_generated" / "logs" / "transcript.jsonl").exists()

    # Check that the DB was copied and cascade_id updated
    db_dst_path = conversations_path / f"{dst_session_id}.db"
    assert db_dst_path.exists()
    conn = sqlite3.connect(db_dst_path)
    cursor = conn.cursor()
    cursor.execute("SELECT cascade_id FROM trajectory_meta")
    rows = cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == dst_session_id
    conn.close()

    # Check that the PB file was copied and updated
    pb_dst_path = conversations_path / f"{dst_session_id}.pb"
    assert pb_dst_path.exists()
    assert pb_dst_path.read_bytes() == f"some prefix {dst_session_id} suffix".encode("utf-8")


@pytest.mark.asyncio
async def test_gemini_operator_turn2_forces_fresh_conv(monkeypatch, tmp_path):
    """The launch/poller-mode invariant for agy NROL-operator turns.

    Operator turns are fresh-conv-by-design: the launch suppresses
    --conversation so each turn reads a fresh tool registry. The poller MUST
    match the launch. Before the fix, turn 2 inherited the turn-1
    cc_session_id (via server.py's resume walk), so use_resume/fork_session
    stayed True while --conversation was suppressed — the poller pinned to
    the OLD session's transcript and structurally skipped the new-UUID
    scan, while agy wrote its real output to a fresh folder the poller
    never inspected. Result: "Antigravity (agy) exited with no response".

    This test is the structural prevention that was missing for three
    operator-parity bugs. It would have caught the regression at commit
    time. See [[agy-operator-turn2-no-response]] and
    [[agy-conversation-resume-gotcha]].
    """
    import gemini_client
    import asyncio

    args_captured = []

    class MockProcess:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.returncode = 0
        async def wait(self):
            return 0

    mock_proc = MockProcess()
    mock_proc.stdout.feed_data(b'{"type":"session_info"}\n')
    mock_proc.stdout.feed_eof()
    mock_proc.stderr.feed_eof()

    async def mock_create_subprocess_exec(*args, **kwargs):
        args_captured.append(args)
        return mock_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create_subprocess_exec)
    monkeypatch.setattr(gemini_client, "_find_agy_exe", lambda: "agy")

    # Redirect brain path to tmp_path (operator override relies on no real
    # ~/.gemini state; the new-UUID scan reads brain_path.iterdir()).
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    brain_path = tmp_path / ".gemini" / "antigravity-cli" / "brain"
    brain_path.mkdir(parents=True, exist_ok=True)

    # A pre-existing turn-1 session with a transcript — the state that makes
    # turn 2 die in the unfixed code. The override must NOT pin to this file.
    stale_session = brain_path / "turn1-session-id"
    stale_logs = stale_session / ".system_generated" / "logs"
    stale_logs.mkdir(parents=True)
    (stale_logs / "transcript.jsonl").write_text(
        '{"type":"step","step_index":0,"content":"turn 1 output"}\n',
        encoding="utf-8",
    )

    # Simulate the real agy writing a fresh UUID folder after launch. The
    # new-UUID scan (else branch) finds this because its name is not in
    # existing_dirs at launch time and its mtime is newer than launch_ts.
    fresh_session = brain_path / "fresh-uuid-from-agy"
    fresh_logs = fresh_session / ".system_generated" / "logs"

    # Register a project id so --project lands in cc_args (operator turns
    # pass --project; without it the launch-invariant assertion is weaker).
    cache_path = tmp_path / ".gemini" / "antigravity-cli" / "cache"
    cache_path.mkdir(parents=True, exist_ok=True)
    import json as _json
    (cache_path / "projects.json").write_text(
        _json.dumps({str(tmp_path.resolve()): "proj-op-123"}), encoding="utf-8"
    )

    # Write the fresh transcript *after* run_gemini records existing_dirs
    # but before the poller scans. We schedule it on the running loop so it
    # lands after launch and before the first poll iteration.
    async def _seed_fresh_transcript():
        await asyncio.sleep(0.05)
        fresh_logs.mkdir(parents=True)
        (fresh_logs / "transcript.jsonl").write_text(
            '{"type":"step","step_index":0,"content":"turn 2 real output"}\n',
            encoding="utf-8",
        )

    # Turn 2 of an operator session: server.py passes the turn-1 session id
    # and fork_session=True. The override must neutralize BOTH.
    async with asyncio.timeout(30):
        task = asyncio.create_task(_seed_fresh_transcript())
        await gemini_client.run_gemini(
            prompt="run a scan",
            cwd=str(tmp_path),
            conv_id=456,
            resume_session_id="turn1-session-id",
            fork_session=True,
            nrol_operator=True,
        )
        await task

    # --- Launch-side invariant: fresh-conv launch ---
    assert len(args_captured) == 1
    cmd_args = args_captured[0]
    assert cmd_args[0] == "agy"
    assert "--conversation" not in cmd_args, (
        "operator turns must not resume an agy conversation (tool registry "
        "freeze + cross-turn compaction) — see [[agy-conversation-resume-gotcha]]"
    )
    assert "--project" in cmd_args, (
        "operator turns must set --project so .agents/mcp_config.json loads"
    )
    # --dangerously-skip-permissions stays (hook is the tool-blocking layer).
    assert "--dangerously-skip-permissions" in cmd_args

    # --- Poller-side invariant: did NOT pin to the stale turn-1 transcript ---
    # No forked brain folder was created (fork-copy block was skipped).
    forked_dirs = [
        p.name for p in brain_path.iterdir()
        if p.is_dir() and p.name not in ("turn1-session-id", "fresh-uuid-from-agy")
    ]
    assert forked_dirs == [], (
        f"operator override must skip fork-copy; found forked dirs: {forked_dirs}"
    )

    # The fresh folder agy actually wrote to exists (the poller's job is to
    # find it, not the stale turn-1 folder).
    assert fresh_logs.exists()
    assert (fresh_logs / "transcript.jsonl").exists()


@pytest.mark.asyncio
async def test_agy_operator_turn2_server_builds_full_history_prompt(tmp_database, monkeypatch):
    """Server-side half of the agy-operator memory invariant.

    The client-side test above (test_gemini_operator_turn2_forces_fresh_conv)
    locks the launch: operator turns drop --conversation so each turn reads
    a fresh tool registry. But that fix ran too late — server.py builds the
    PROMPT before run_gemini is called. For non-agy providers the resume
    short-circuit (latest_user_content only) is correct: codex holds
    history in a stateful thread/fork, claude in --resume. agy has neither
    (fresh stdio process per turn), so the one-line prompt landed on a
    process with zero conversation history → amnesia ("what was the first
    message I sent?" could not be answered).

    This test locks the server-side fix: for an agy-operator turn 2, the
    prompt passed to gemini_client.run_gemini must contain the FULL rebuilt
    branch history, not just the latest user message. It would have caught
    the amnesia regression when --conversation was dropped for operators.

    See [[agy-operator-turn2-no-response]].
    """
    import asyncio
    import server
    import database as db
    import gemini_client

    # --- Seed a 2-turn operator conversation in the branch ---
    conv = await db.create_conversation(
        "Hormuz Operator", mode="gemini", project_dir="."
    )
    await db.update_conversation_fields(conv["id"], nrol_operator=1, cc_model="gemini-3.5-flash")

    # Turn 1: user asks for a scan, assistant replies with a briefing.
    u1 = await db.add_message(conv["id"], "user", "run a news scan on topic slug-hormuz")
    # Persist a turn-1 session id + gemini model so the resume walk finds it
    # and the cross-provider check (server.py ~4872-4886) sees prev_is_gemini.
    a1 = await db.add_message(
        conv["id"], "assistant", "Scan complete: 3 articles parked for slug-hormuz.",
        parent_id=u1["id"], cc_session_id="turn1-agy-session",
    )
    await db.update_message_content(a1["id"], cc_model_used="gemini-3.5-flash")

    # Turn 2: user references prior context — amnesia check.
    u2 = await db.add_message(
        conv["id"], "user", "commit the parked matches for slug-hormuz", parent_id=a1["id"]
    )

    # --- Stub run_gemini at the server module's reference, capture prompt ---
    captured: dict = {}

    async def fake_run_gemini(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs

        class _Proc:
            returncode = 0
            pid = 12345

            async def wait(self):
                return 0

        async def _events():
            # Minimal terminal event so the handler's stream loop completes.
            yield {"type": "result", "is_error": False, "result_text": "done",
                   "session_id": "turn2-agy-session"}

        return _Proc(), _events()

    monkeypatch.setattr(server.gemini_client, "run_gemini", fake_run_gemini)
    # Avoid touching the real filesystem for model settings writes.
    monkeypatch.setattr(gemini_client, "_set_agy_model", lambda *_a, **_k: None)
    monkeypatch.setattr(gemini_client, "_configure_permission_hook", lambda *_a, **_k: None)
    monkeypatch.setattr(gemini_client, "_configure_operator", lambda *_a, **_k: None)

    # --- Drive the generation handler (websocket=None: _ws_send skips silently) ---
    await server._handle_claude_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"], "cc_model": "gemini-3.5-flash"},
    )

    # --- The invariant: prompt contains FULL history, not just the one-liner ---
    assert "prompt" in captured, "run_gemini was never called — handler exited early"
    prompt = captured["prompt"]

    # Prior-turn content is present (the amnesia bug would have dropped these).
    assert "run a news scan on topic slug-hormuz" in prompt, (
        "turn-1 user message missing from prompt — agy-operator amnesia: "
        "server.py sent only the latest user message instead of full history"
    )
    assert "Scan complete: 3 articles parked" in prompt, (
        "turn-1 assistant output missing from prompt — full history not rebuilt"
    )
    # And the latest turn-2 message is present too.
    assert "commit the parked matches for slug-hormuz" in prompt

    # resume_session_id still propagates to the client (the client-side
    # override at gemini_client.py:489 relies on it for logging + the
    # fresh-conv invariant test above depends on it being passed).
    assert captured["kwargs"].get("resume_session_id") == "turn1-agy-session"
    assert captured["kwargs"].get("nrol_operator") is True


def test_codex_client_resume_and_fork(tmp_path):
    import codex_client

    method, params = codex_client._codex_thread_request(
        cwd=str(tmp_path),
        codex_model="gpt-4o",
        approval_policy="on-request",
        sandbox_mode="workspace-write",
        thread_config={"mcp_servers": {"web-tools": {}}},
        resume_session_id="parent_session",
        fork_session=True,
    )

    assert method == "thread/fork"
    assert params["threadId"] == "parent_session"
    assert params["config"] == {"mcp_servers": {"web-tools": {}}}
    assert "sessionStartSource" not in params
    assert "threadSource" not in params

    method, params = codex_client._codex_thread_request(
        cwd=str(tmp_path),
        codex_model="gpt-4o",
        approval_policy="on-request",
        sandbox_mode="workspace-write",
        resume_session_id="parent_session",
        fork_session=False,
    )
    assert method == "thread/resume"
    assert params["threadId"] == "parent_session"

    method, params = codex_client._codex_thread_request(
        cwd=str(tmp_path),
        codex_model="gpt-4o",
        approval_policy="on-request",
        sandbox_mode="workspace-write",
    )
    assert method == "thread/start"
    assert params["sessionStartSource"] == "startup"
    assert params["threadSource"] == "user"
