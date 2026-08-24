"""Cross-provider Loom user-contract invariants."""

from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent


def _assistant(message_id, mode, session_id, model="", content="reply"):
    return {
        "id": message_id,
        "role": "assistant",
        "content": content,
        "cc_session_id": session_id,
        "cc_session_mode": mode,
        "cc_model_used": model,
    }


@pytest.mark.parametrize("target_mode", ["claude", "codex", "gemini", "goose", "hermes", "dream"])
def test_session_contract_resumes_only_the_nearest_compatible_assistant(target_mode):
    from provider_contract import select_resume_session

    decision = select_resume_session(
        [
            {"id": 1, "role": "user", "content": "first"},
            _assistant(2, target_mode, "session-nearest"),
            {"id": 3, "role": "user", "content": "next"},
        ],
        target_mode,
    )

    assert decision.can_resume
    assert decision.session_id == "session-nearest"
    assert decision.reason == "resume"


@pytest.mark.parametrize("target_mode,foreign_mode", [
    ("claude", "codex"),
    ("codex", "gemini"),
    ("gemini", "claude"),
    ("goose", "codex"),
    ("hermes", "dream"),
    ("dream", "hermes"),
])
def test_session_contract_never_resurrects_a_session_across_a_to_b_to_a(
    target_mode, foreign_mode
):
    from provider_contract import select_resume_session

    decision = select_resume_session(
        [
            _assistant(1, target_mode, "old-target-session"),
            {"id": 2, "role": "user", "content": "switch"},
            _assistant(3, foreign_mode, "intervening-session"),
            {"id": 4, "role": "user", "content": "switch back"},
        ],
        target_mode,
    )

    assert not decision.can_resume
    assert decision.reason == "provider_boundary"
    assert decision.message_id == 3


def test_session_contract_model_switch_and_legacy_rows_force_replay():
    from provider_contract import select_resume_session

    model_switch = select_resume_session(
        [_assistant(1, "goose", "old", model="goose:openrouter:model-a")],
        "goose",
        target_model="goose:openrouter:model-b",
        models_match=lambda recorded, target: recorded == target,
    )
    legacy = select_resume_session(
        [_assistant(2, None, "unscoped")],
        "dream",
    )

    assert model_switch.reason == "model_boundary"
    assert not model_switch.can_resume
    assert legacy.reason == "legacy_unscoped_session"
    assert not legacy.can_resume


def test_session_contract_compact_and_invalid_assistant_turns_are_boundaries():
    from provider_contract import select_resume_session

    compact = select_resume_session(
        [
            _assistant(1, "claude", "old"),
            {"id": 2, "role": "system", "content": "[CC context compactified (handoff)]"},
            {"id": 3, "role": "user", "content": "continue"},
        ],
        "claude",
    )
    errored = select_resume_session(
        [
            _assistant(1, "claude", "old"),
            _assistant(2, "claude", "bad", content="[Error: failed]"),
        ],
        "claude",
    )

    assert compact.reason == "compact_boundary"
    assert errored.reason == "assistant_error"
    assert not compact.can_resume
    assert not errored.can_resume


def test_model_picker_never_silently_replaces_an_unavailable_selector():
    source = (REPO / "static" / "app.js").read_text(encoding="utf-8")

    assert "prev = 'sonnet'" not in source
    assert "(currently unavailable)" in source
    assert "sel.value = prev" in source


def test_historical_harness_label_prefers_message_model_over_current_mode():
    source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "if (mode === 'goose' || msgModel.startsWith('goose:'))" not in source
    assert "if (msgModel.startsWith('goose:')) return 'goose';" in source
    assert "recorded model is stronger provenance" in source


def test_antigravity_only_accepts_provider_confirmed_model_identity():
    source = (REPO / "server.py").read_text(encoding="utf-8")

    assert 'evt.get("model_confirmed", True)' in source
    assert '"stage": "model_confirmation"' in source
    assert "Antigravity model mismatch" in source


def test_unknown_selector_has_no_implicit_harness():
    import server

    assert server._mode_for_cc_model("not-a-provider-selector") is None
    assert server._mode_for_cc_model("claude-opus-4-6") == "claude"
    assert server._mode_for_cc_model("gemini:gemini-3.7-flash-high") == "gemini"
    assert server._mode_for_cc_model("codex-gpt-5.6-sol") == "codex"
    assert server._mode_for_cc_model("goose:openrouter:z-ai/glm-5.2") == "goose"


@pytest.mark.asyncio
async def test_conversation_update_rejects_unknown_selector(tmp_database):
    import database as db
    import server

    conv = await db.create_conversation("Selector Contract", mode="claude")
    with pytest.raises(server.HTTPException) as exc:
        await server.api_update_conversation(
            conv["id"], {"cc_model": "not-a-provider-selector"}
        )

    assert exc.value.status_code == 400
    assert "Unknown model selector" in exc.value.detail
    saved = await db.get_conversation(conv["id"])
    assert saved["cc_model"] == "sonnet"


@pytest.mark.asyncio
async def test_shared_handler_switch_back_replays_intervening_codex_turn(
    monkeypatch, tmp_database, tmp_path
):
    import database as db
    import server

    conv = await db.create_conversation(
        "Claude A-B-A", mode="claude", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], cc_model="claude-opus-4-6")
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(
        conv["id"], "assistant", "old claude reply", parent_id=u1["id"],
        cc_session_id="claude-old",
    )
    await db.update_message_content(
        a1["id"], cc_session_mode="claude", cc_model_used="claude-opus-4-6"
    )
    u2 = await db.add_message(
        conv["id"], "user", "ask codex", parent_id=a1["id"]
    )
    c1 = await db.add_message(
        conv["id"], "assistant", "intervening codex reply", parent_id=u2["id"],
        cc_session_id="codex-middle",
    )
    await db.update_message_content(
        c1["id"], cc_session_mode="codex", cc_model_used="gpt-5.6-sol"
    )
    u3 = await db.add_message(
        conv["id"], "user", "back to claude", parent_id=c1["id"]
    )

    captured = {}

    async def fake_run_claude(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        captured["resume_session_id"] = kwargs.get("resume_session_id")

        class Proc:
            pid = 9123
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {
                "type": "session_info",
                "session_id": "claude-new",
                "model": "claude-opus-4-6",
            }
            yield {"type": "text_delta", "text": "replayed"}
            yield {
                "type": "result",
                "session_id": "claude-new",
                "is_error": False,
                "result_text": "replayed",
            }

        return Proc(), events()

    monkeypatch.setattr(server.claude_client, "run_claude", fake_run_claude)

    await server._handle_claude_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={
            "action": "generate",
            "parent_id": u3["id"],
            "cc_model": "claude-opus-4-6",
        },
    )

    assert captured["resume_session_id"] is None
    assert "old claude reply" in captured["prompt"]
    assert "intervening codex reply" in captured["prompt"]
    assert "back to claude" in captured["prompt"]


@pytest.mark.asyncio
async def test_compact_handoff_does_not_search_past_provider_boundary(
    monkeypatch, tmp_database, tmp_path
):
    import database as db
    import server

    conv = await db.create_conversation(
        "Compact A-B-A", mode="claude", project_dir=str(tmp_path)
    )
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(
        conv["id"], "assistant", "old claude", parent_id=u1["id"],
        cc_session_id="claude-old",
    )
    await db.update_message_content(
        a1["id"], cc_session_mode="claude", cc_model_used="claude-opus-4-6"
    )
    u2 = await db.add_message(conv["id"], "user", "switch", parent_id=a1["id"])
    c1 = await db.add_message(
        conv["id"], "assistant", "codex middle", parent_id=u2["id"],
        cc_session_id="codex-middle",
    )
    await db.update_message_content(
        c1["id"], cc_session_mode="codex", cc_model_used="gpt-5.6-sol"
    )
    u3 = await db.add_message(conv["id"], "user", "continue", parent_id=c1["id"])

    async def fail_run_claude(*args, **kwargs):
        raise AssertionError("compaction must not resurrect the old Claude session")

    monkeypatch.setattr(server.claude_client, "run_claude", fail_run_claude)

    result = await server._run_compact_handoff(
        conv["id"],
        await db.get_conversation(conv["id"]),
        u3["id"],
        "claude-opus-4-6",
        "high",
        str(tmp_path),
    )

    assert result == (None, None, None)
