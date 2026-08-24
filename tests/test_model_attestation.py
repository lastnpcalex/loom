import json
from pathlib import Path

import pytest


def test_claude_code_records_harness_then_provider_model_evidence():
    import claude_client

    state = {
        "requested_model": "sonnet",
        "launch_model": "sonnet",
        "model_provider": "anthropic",
    }
    init = claude_client._process_event(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "claude-session",
            "model": "claude-sonnet-4-7",
        },
        state,
    )[0]
    provider = claude_client._process_event(
        {
            "type": "stream_event",
            "event": {
                "type": "message_start",
                "message": {"model": "claude-sonnet-4-7"},
            },
        },
        state,
    )[0]

    assert init["model_attestation"]["status"] == "verified"
    assert init["model_attestation"]["harness"] == "Claude Code"
    assert init["model_attestation"]["verification_level"] == "harness"
    assert provider["model_attestation"]["verification_level"] == "provider_response"
    assert provider["model_attestation"]["effective_model"] == "claude-sonnet-4-7"


def test_antigravity_attestation_distinguishes_launch_from_runtime():
    import gemini_client

    configured = gemini_client._agy_model_attestation(
        "gemini:gemini-3.7-flash-high",
        "gemini-3.7-flash-high",
        None,
        source="agy_cli_launch_arguments",
    )
    verified = gemini_client._agy_model_attestation(
        "gemini:gemini-3.7-flash-high",
        "gemini-3.7-flash-high",
        "gemini-3.7-flash-high",
        source="antigravity_stream_init",
    )

    assert configured["status"] == "configured"
    assert verified["status"] == "verified"
    assert verified["harness"] == "Antigravity (agy)"
    assert verified["model_provider"] == "google"


def test_goose_does_not_mislabel_process_configuration_as_verification():
    import goose_client

    configured = goose_client._goose_model_attestation(
        "goose:openrouter:z-ai/glm-5.2"
    )
    verified = goose_client._goose_model_attestation(
        "goose:openrouter:z-ai/glm-5.2",
        "openrouter:z-ai/glm-5.2",
        session_id="goose-session",
    )

    assert configured["status"] == "configured"
    assert configured["effective_model"] is None
    assert configured["source"] == "goose_process_environment"
    assert verified["status"] == "verified"
    assert verified["effective_model"] == "openrouter:z-ai/glm-5.2"


def test_hermes_attestation_records_acp_model_acknowledgement():
    import hermes_client

    attestation = hermes_client._hermes_model_attestation(
        "qwen3.6:27b",
        "custom:qwen3.6:27b",
        "custom:qwen3.6:27b",
        source="hermes_acp_set_model_ack",
        session_id="hermes-session",
        verified=True,
    )

    assert attestation["status"] == "verified"
    assert attestation["harness"] == "Hermes ACP"
    assert attestation["model_provider"] == "custom"


def test_direct_completion_attestation_uses_response_model():
    import llama_client

    verified = llama_client._completion_model_attestation(
        "Qwen3.6-27B.gguf",
        "Qwen3.6-27B.gguf",
        "Qwen3.6-27B.gguf",
        provider="llama-server",
        source="openai_compatible_stream_chunk",
    )
    mismatch = llama_client._completion_model_attestation(
        "openrouter:z-ai/glm-5.2",
        "z-ai/glm-5.2",
        "openai/gpt-5.6-luna",
        provider="openrouter",
        source="openrouter_stream_chunk",
    )

    assert verified["status"] == "verified"
    assert verified["harness"] == "Llama Server"
    assert mismatch["status"] == "mismatch"


@pytest.mark.asyncio
async def test_server_persists_and_broadcasts_turn_model_attestation(
    tmp_database, monkeypatch
):
    import database as db
    import server

    sent = []

    async def fake_ws_send(conv_id, payload):
        sent.append((conv_id, payload))

    monkeypatch.setattr(server, "_ws_send", fake_ws_send)
    conv = await db.create_conversation("Model identity", mode="claude")
    parent = await db.add_message(conv["id"], "user", "hello")
    draft = await db.add_message(
        conv["id"], "assistant", "", parent_id=parent["id"]
    )
    attestation = {
        "status": "verified",
        "harness": "Claude Code",
        "requested_model": "sonnet",
        "launch_model": "sonnet",
        "effective_model": "claude-sonnet-4-7",
        "model_provider": "anthropic",
        "source": "anthropic_compatible_message_start",
        "verification_level": "provider_response",
    }

    await server._record_turn_model_attestation(
        conv["id"], draft["id"], parent["id"], attestation
    )

    saved = await db.get_message(draft["id"])
    assert json.loads(saved["model_attestation"]) == attestation
    assert sent[-1][1]["type"] == "model_attestation"
    assert sent[-1][1]["attestation"]["harness"] == "Claude Code"


def test_model_attestation_ui_labels_harness_and_evidence_level():
    source = (Path(__file__).resolve().parent.parent / "static" / "chat.js").read_text(
        encoding="utf-8"
    )

    assert "configured" in source
    assert "['Harness', att.harness]" in source
    assert "['Verification', att.verification_level]" in source
    assert "${escapeHtml(harness)} · ${escapeHtml(model)}" in source
    assert "Goose process configuration" in source


def test_legacy_codex_attestation_recovers_harness_from_evidence_source():
    import server

    normalized = server._normalize_turn_model_attestation(
        {
            "status": "verified",
            "requested_model": "codex-gpt-5.6-sol",
            "launch_model": "gpt-5.6-sol",
            "effective_model": "gpt-5.6-sol",
            "model_provider": "openai",
            "source": "codex_app_server_thread_response",
        }
    )

    assert normalized["harness"] == "Codex app-server"
    assert normalized["verification_level"] == "harness"

    source = (Path(__file__).resolve().parent.parent / "static" / "chat.js").read_text(
        encoding="utf-8"
    )
    assert "codex_app_server_thread_response: ['Codex app-server', 'harness']" in source
    assert "sourceDefaults?.[0] || _harnessLabelForModel(model)" in source


def test_streaming_draft_and_attestation_survive_live_repaints():
    source = (Path(__file__).resolve().parent.parent / "static" / "chat.js").read_text(
        encoding="utf-8"
    )

    assert "function _ensureLiveStreamDraft(data = {})" in source
    assert "_appendLiveDraftText(data.content)" in source
    assert "_appendLiveDraftThinking(data.content)" in source
    assert "_mergeLiveStreamDraftIntoState();" in source
    assert "appendStreamingMessage(lastMsg.cc_model_used, lastMsg.model_attestation, lastMsg.id)" in source
    assert "draft.model_attestation = JSON.stringify(attestation)" in source


def test_all_generation_families_persist_model_attestations():
    source = (Path(__file__).resolve().parent.parent / "server.py").read_text(
        encoding="utf-8"
    )

    # Shared Claude/Codex/Antigravity path, Goose, three Hermes-class paths,
    # OODA sync (including repair), and direct Weave streaming.
    assert source.count("_record_turn_model_attestation(") >= 9
