"""Multi-provider NROL operator parity (ROADMAP.md "Multi-provider operator parity").

Unit layer only: launch-parameter builders and the provider guard. The live
acceptance checks — sandbox blocks a topic-JSON write without prompting, MCP
commit raises the Loom browser approval, operator recites OPERATOR.md — stay
manual per provider.
"""

import json
from pathlib import Path

import pytest


# --- Image-gate invariant ----------------------------------------------------
# The describe_image pre-flight (server.py ~5025 & ~5646) and the "use
# describe_image, don't Read images" system-prompt warning
# (claude_client._loom_append_system_prompt, use_llama-only) are THREE legs of
# one stool: a provider needs the text-summary detour IFF it lacks native
# Anthropic image-content transport. umans rides the same transport as Claude
# (api.code.umans.ai via claude_client.run_claude), so Read delivers image
# bytes to it directly — the pre-flight would replace pixels with a worse text
# note AND inject a "do NOT read image files" header (server.py ~5083),
# blinding the model. The bug was the two gates disagreeing for umans:
# warning=use_llama only, pre-flight=use_llama OR use_umans. This invariant
# would have caught it. See [[loom-recurring-bug-shapes]] (multi-file
# invariant class) and [[ground-claims-in-code]].

def _describe_preflight_gates() -> dict[str, bool]:
    """Static source check: which use_* flag each describe gate keys on.

    Reads server.py text rather than driving the websocket generation flow —
    the invariant is about the *source-level agreement* between the pre-flight
    gates and the system-prompt warning, not runtime dispatch. A brittle
    end-to-end harness would couple to message persistence and break on
    unrelated changes; this catches the drift class directly.
    """
    import server

    src = Path(server.__file__).read_text(encoding="utf-8")
    # Both pre-flight gates must be `use_llama`-only, matching the warning.
    # The original bug had `use_llama or use_umans` here.
    has_umans_gate = "use_llama or use_umans" in src
    return {
        "preflight_has_umans_gate": has_umans_gate,
        "primary_gate_llama_only": "if use_llama and image_files:" in src,
        "resume_gate_llama_only": src.count("if use_llama and image_files:") >= 2,
    }


def test_image_describe_preflight_excludes_umans():
    """umans takes the Claude path: native image content blocks via Read, no
    describe_image pre-flight. The pre-flight gates must NOT mention use_umans."""
    gates = _describe_preflight_gates()
    assert not gates["preflight_has_umans_gate"], (
        "describe_image pre-flight is gated on use_umans — umans uses the same "
        "Anthropic transport as Claude (Read delivers pixels natively), so a "
        "pre-flight text summary both replaces the image and injects a "
        "'do NOT read image files' header that blinds the model. See "
        "[[loom-recurring-bug-shapes]] multi-file invariant."
    )
    assert gates["primary_gate_llama_only"], (
        "primary pre-flight gate no longer reads `if use_llama and image_files:` — "
        "the gate was restructured; update this invariant test deliberately"
    )
    assert gates["resume_gate_llama_only"], (
        "resume/re-attach pre-flight gate (the second `if use_llama and image_files:`) "
        "is missing or restructured — both gates must stay in lockstep"
    )


def _image_warning_gate_is_llama_only() -> bool:
    """True iff the 'use describe_image, don't Read images' system-prompt
    warning is gated on `use_llama` only (not also use_umans).

    Scoped to the image_warning block specifically. `use_llama or use_umans`
    appears LEGITIMATELY elsewhere in claude_client.py (the WebSearch/WebFetch
    block and MCP web-tools registration — umans genuinely lacks built-in web
    search, same as llama). Those shared gates are correct; only the image
    warning must be llama-only, because umans takes native image content blocks
    through the same Anthropic transport as Claude.
    """
    import claude_client
    import re

    src = Path(claude_client.__file__).read_text(encoding="utf-8")
    # The warning lives in _loom_append_system_prompt. Extract the if-block that
    # assigns image_warning so we only judge the image gate, not the web gates.
    m = re.search(r"(if use_llama[^:\n]*:\s*\n\s*image_warning\s*=\s*\()", src)
    if not m:
        return False  # warning block restructured — investigate deliberately
    # Take the block from the `if` through the contract append that closes it.
    start = m.start()
    end = src.find("return merge_system_prompts", start)
    block = src[start:end] if end != -1 else src[start:]
    return "use_umans" not in block


def test_image_warning_gate_matches_preflight_gate():
    """The invariant: the set of providers warned (use_llama) MUST equal the
    set of providers pre-described (use_llama). The two gates live in different
    files (claude_client.py vs server.py) with no call linking them — this
    assertion is the only enforcement that they agree.

    The warning block must be `use_llama`-only AND the pre-flight must be
    `use_llama`-only. Asserting both keeps a new visionless provider from being
    added to one gate and not the other.
    """
    warning_is_llama_only = _image_warning_gate_is_llama_only()
    preflight_is_llama_only = not _describe_preflight_gates()["preflight_has_umans_gate"]

    assert warning_is_llama_only, (
        "system-prompt image warning in claude_client is no longer `use_llama`-only "
        "or also fires for umans — the warning and the pre-flight must agree on "
        "which providers get the visionless text-summary detour"
    )
    assert preflight_is_llama_only, (
        "describe pre-flight in server.py is not `use_llama`-only — see "
        "test_image_describe_preflight_excludes_umans for the transport rationale"
    )



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


# --------------------------------------------------------------------------- #
# Dream/Hermes cross-mode session isolation (RC2)
# --------------------------------------------------------------------------- #
# The resume walk in _handle_dream_generation must skip a cc_session_id whose
# cc_session_mode is non-NULL and not "dream" — a Hermes (llama) session id is
# meaningless in the Dream home's state.db, and forking it there is how a
# foreign conversation's tree leaks into a Dream session. NULL mode = legacy,
# treated as compatible so existing conversations keep resuming.

async def test_dream_resume_walk_skips_foreign_mode_session(monkeypatch):
    """A branch carrying a Hermes-mode session id must NOT be forked for Dream."""
    import server
    import database as db

    conv = await db.create_conversation("Dream Isolation", mode="dream", project_dir=".")
    u1 = await db.add_message(conv["id"], "user", "first turn")
    # Prior assistant message tagged as a HERMES session — e.g. the conv was
    # run as Hermes mode before being switched to Dream.
    a1 = await db.add_message(
        conv["id"], "assistant", "hermes reply", parent_id=u1["id"],
        cc_session_id="hermes-sess-1",
    )
    await db.update_message_content(a1["id"], cc_session_mode="hermes")
    u2 = await db.add_message(conv["id"], "user", "dream turn", parent_id=a1["id"])

    captured: dict = {}

    async def fake_run_hermes(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        captured["resume_session_id"] = kwargs.get("resume_session_id")
        captured["is_first_turn"] = kwargs.get("is_first_turn", True)

        class _Proc:
            returncode = 0
            pid = 4321
            async def wait(self):
                return 0

        async def _events():
            yield {"type": "session_info", "session_id": "dream-sess-new",
                   "model": "diffusiongemma"}
            yield {"type": "text_delta", "text": "dream reply"}
            yield {"type": "result", "session_id": "dream-sess-new",
                   "stop_reason": "end_turn", "duration_ms": 10, "num_turns": 1}

        return _Proc(), _events()

    monkeypatch.setattr(server.hermes_client, "run_hermes", fake_run_hermes)
    # Avoid touching the filesystem for Dream home creation.
    monkeypatch.setattr(server, "_ensure_dream_hermes_home", lambda: "/tmp/dream-home")

    await server._handle_dream_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    # The Hermes-mode session id was skipped → no resume, fresh session/new,
    # and is_first_turn must be True (full orientation for the fresh session).
    assert captured["resume_session_id"] is None, (
        "Dream resume walk forked a foreign-mode (hermes) session id — "
        "this is the cross-context leak. resume_session_id should be None."
    )
    assert captured["is_first_turn"] is True, (
        "Fresh session after skipping a foreign-mode id needs full orientation."
    )


async def test_dream_resume_walk_picks_up_same_mode_session(monkeypatch):
    """A branch carrying a Dream-mode session id IS resumed, with is_first_turn=False."""
    import server
    import database as db

    conv = await db.create_conversation("Dream Resume", mode="dream", project_dir=".")
    u1 = await db.add_message(conv["id"], "user", "first turn")
    a1 = await db.add_message(
        conv["id"], "assistant", "dream reply", parent_id=u1["id"],
        cc_session_id="dream-sess-1",
    )
    await db.update_message_content(a1["id"], cc_session_mode="dream")
    u2 = await db.add_message(conv["id"], "user", "second turn", parent_id=a1["id"])

    captured: dict = {}

    async def fake_run_hermes(prompt, *args, **kwargs):
        captured["resume_session_id"] = kwargs.get("resume_session_id")
        captured["is_first_turn"] = kwargs.get("is_first_turn", True)
        captured["prompt"] = prompt

        class _Proc:
            returncode = 0
            pid = 4322
            async def wait(self):
                return 0

        async def _events():
            yield {"type": "session_info", "session_id": "dream-sess-2",
                   "model": "diffusiongemma"}
            yield {"type": "text_delta", "text": "continued"}
            yield {"type": "result", "session_id": "dream-sess-2",
                   "stop_reason": "end_turn", "duration_ms": 10, "num_turns": 1}

        return _Proc(), _events()

    monkeypatch.setattr(server.hermes_client, "run_hermes", fake_run_hermes)
    monkeypatch.setattr(server, "_ensure_dream_hermes_home", lambda: "/tmp/dream-home")

    await server._handle_dream_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    # Same-mode session is resumed (forked) and is_first_turn=False — no
    # <loom_branch_info> duplication since the fork already holds the history.
    assert captured["resume_session_id"] == "dream-sess-1"
    assert captured["is_first_turn"] is False
    # Bare continuation prompt only — the latest user message, not the full history.
    assert captured["prompt"] == "second turn"


async def test_dream_resume_walk_null_mode_is_compatible(monkeypatch):
    """Legacy rows with NULL cc_session_mode are treated as compatible (resume)."""
    import server
    import database as db

    conv = await db.create_conversation("Dream Legacy", mode="dream", project_dir=".")
    u1 = await db.add_message(conv["id"], "user", "first turn")
    # No cc_session_mode tagged — legacy row predating the migration.
    a1 = await db.add_message(
        conv["id"], "assistant", "legacy reply", parent_id=u1["id"],
        cc_session_id="legacy-sess-1",
    )
    u2 = await db.add_message(conv["id"], "user", "next turn", parent_id=a1["id"])

    captured: dict = {}

    async def fake_run_hermes(prompt, *args, **kwargs):
        captured["resume_session_id"] = kwargs.get("resume_session_id")
        captured["is_first_turn"] = kwargs.get("is_first_turn", True)

        class _Proc:
            returncode = 0
            pid = 4323
            async def wait(self):
                return 0

        async def _events():
            yield {"type": "session_info", "session_id": "dream-sess-3",
                   "model": "diffusiongemma"}
            yield {"type": "text_delta", "text": "ok"}
            yield {"type": "result", "session_id": "dream-sess-3",
                   "stop_reason": "end_turn", "duration_ms": 10, "num_turns": 1}

        return _Proc(), _events()

    monkeypatch.setattr(server.hermes_client, "run_hermes", fake_run_hermes)
    monkeypatch.setattr(server, "_ensure_dream_hermes_home", lambda: "/tmp/dream-home")

    await server._handle_dream_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    # NULL mode = legacy/unscoped → compatible, so the session is resumed.
    assert captured["resume_session_id"] == "legacy-sess-1"
    assert captured["is_first_turn"] is False


# --- agy planner-loop / sustained-dead detection -----------------------------
# Regression coverage for the "agy dies but Loom hangs forever" fix in
# gemini_client.py. agy's planner-loop shutdown logs "Language server shutting
# down" but the OS process never exits (proc.returncode stays None), so the
# EOF break never fires and Loom would hang emitting "Working (Ns)" forever.
# The fix detects the stall signature in _scan_agy_log_for_error and a
# sustained-dead-log condition in the heartbeat. These tests mock the agy
# subprocess and write fake CLI logs + transcripts; agy is never invoked.

def _agy_log_filename_for_now():
    """Build a cli-YYYYMMDD_HHMMSS.log filename whose parsed ts is ~now."""
    import time
    t = time.localtime()
    return time.strftime("cli-%Y%m%d_%H%M%S.log", t)


def _write_fake_agy_log(tmp_path, body: str):
    """Write a fake agy cli log so _scan_agy_log_for_error / _is_agy_alive find it."""
    log_dir = tmp_path / ".gemini" / "antigravity-cli" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / _agy_log_filename_for_now()
    p.write_text(body, encoding="utf-8")
    return p


def _write_fake_transcript(tmp_path, conv_id, lines):
    """Write a fake transcript.jsonl under the brain/<conv_id>/ path the tailer scans."""
    base = tmp_path / ".gemini" / "antigravity-cli" / "brain" / str(conv_id)
    logs = base / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    p = logs / "transcript.jsonl"
    p.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    return p


class _StuckMockProcess:
    """Mock agy process whose returncode stays None (simulates agy's
    stuck-but-not-exited state after planner-loop graceful shutdown)."""

    def __init__(self):
        import asyncio
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None  # never exits — the bug's core shape

    async def wait(self):
        # Hangs forever (as the real agy does); the kill path uses proc.kill()
        import asyncio
        await asyncio.Future()

    def kill(self):
        self.returncode = -9


def _patch_agy_subprocess(monkeypatch, mock_proc):
    import asyncio
    import gemini_client

    async def mock_create_subprocess_exec(*args, **kwargs):
        return mock_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create_subprocess_exec)
    monkeypatch.setattr(gemini_client, "_find_agy_exe", lambda: "agy")
    return gemini_client


def test_scan_agy_log_detects_planner_stall(tmp_path, monkeypatch):
    """_scan_agy_log_for_error returns a planner-stall message when >=10
    'PlannerResponse without ModifiedResponse' lines + both shutdown-cascade
    lines are present, and returns None for a benign log."""
    import time
    import gemini_client

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    # Benign log: a few stall lines, no shutdown cascade → not fatal
    benign = ("PlannerResponse without ModifiedResponse encountered\n" * 5)
    _write_fake_agy_log(tmp_path, benign)
    launch_ts = time.time()
    assert gemini_client._scan_agy_log_for_error(launch_ts) is None

    # Fatal log: >=10 stall lines + both cascade lines → planner-stall error
    fatal = (
        "PlannerResponse without ModifiedResponse encountered\n" * 50
        + "conversation_manager.go:478] Stopping conversation stream\n"
        + "server.go:2308] Language server shutting down\n"
    )
    _write_fake_agy_log(tmp_path, fatal)
    err = gemini_client._scan_agy_log_for_error(launch_ts)
    assert err is not None
    assert "planner loop stalled" in err.lower()
    assert "50" in err  # stall count surfaced


@pytest.mark.asyncio
async def test_tailer_breaks_on_planner_stall_signature(tmp_path, monkeypatch):
    """When agy's log shows the planner-stall shutdown signature, the tailer
    breaks (doesn't hang) and the result event carries is_error with the
    planner-stall message — even though proc.returncode stays None."""
    import asyncio
    import time

    mock_proc = _StuckMockProcess()
    gemini_client = _patch_agy_subprocess(monkeypatch, mock_proc)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    # A transcript with one PLANNER_RESPONSE step so full_text gets content
    # (verifies the error scan runs even after partial text). Then the tailer
    # hits EOF and the heartbeat fires every ~1.5s.
    _write_fake_transcript(tmp_path, conv_id=7, lines=[
        '{"type":"PLANNER_RESPONSE","step_index":1,"content":"partial thinking"}',
    ])

    # Pre-write the fatal agy log (stall signature already present).
    fatal = (
        "PlannerResponse without ModifiedResponse encountered\n" * 50
        + "conversation_manager.go:478] Stopping conversation stream\n"
        + "server.go:2308] Language server shutting down\n"
    )
    _write_fake_agy_log(tmp_path, fatal)

    # Drive run_gemini with an outer timeout — must complete, not hang.
    proc, event_stream = await asyncio.wait_for(
        gemini_client.run_gemini(
            prompt="test",
            cwd=str(tmp_path),
            conv_id=7,
        ),
        timeout=20,
    )

    events = []
    async for evt in event_stream:
        events.append(evt)
        if evt is None:
            break
        if isinstance(evt, dict) and evt.get("type") == "result":
            break

    result = next(e for e in events if isinstance(e, dict) and e.get("type") == "result")
    assert result["is_error"] is True
    assert "planner loop stalled" in (result.get("error") or "").lower()


@pytest.mark.asyncio
async def test_tailer_breaks_on_sustained_dead_log(tmp_path, monkeypatch):
    """When agy's log goes stale (no mtime change) for 60s+ with no active
    tool call and proc.returncode still None, the sustained-dead detector
    breaks the tailer instead of hanging forever."""
    import asyncio
    import time
    import gemini_client

    mock_proc = _StuckMockProcess()
    _patch_agy_subprocess(monkeypatch, mock_proc)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    _write_fake_transcript(tmp_path, conv_id=8, lines=[
        '{"type":"PLANNER_RESPONSE","step_index":1,"content":"partial"}',
    ])

    # A benign log (no stall signature) so the planner detector doesn't fire —
    # only the sustained-dead path should trigger. Set its mtime 70s in the
    # past so _is_agy_alive returns False immediately.
    log_path = _write_fake_agy_log(tmp_path, "I0706 20:00:00.000000 1 server.go:1322] Starting\n")
    stale = time.time() - 70
    import os
    os.utime(log_path, (stale, stale))

    # Fast-forward time so the 60s sustained-dead window elapses in ~2s of
    # real test time. The heartbeat checks every 30 polls (~1.5s); two
    # heartbeats past the threshold is enough. _is_agy_alive and the heartbeat
    # both call time.time() via a local `import time as _time`, so patching
    # time.time on the time module itself reaches all call sites.
    real_time = time.time
    t0 = real_time()

    def fast_time():
        # Advance ~35s per real second, so 60s sustained threshold is crossed
        # in under 2s of wall-clock test time.
        return t0 + (real_time() - t0) * 35.0

    monkeypatch.setattr(time, "time", fast_time)

    proc, event_stream = await asyncio.wait_for(
        gemini_client.run_gemini(
            prompt="test",
            cwd=str(tmp_path),
            conv_id=8,
        ),
        timeout=20,
    )

    events = []
    async for evt in event_stream:
        events.append(evt)
        if evt is None:
            break
        if isinstance(evt, dict) and evt.get("type") == "result":
            break

    result = next(e for e in events if isinstance(e, dict) and e.get("type") == "result")
    assert result["is_error"] is True
    assert "shut down" in (result.get("error") or "").lower() or "no log activity" in (result.get("error") or "").lower()
