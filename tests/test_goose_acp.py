"""Goose ACP integration invariants."""

import pytest


def test_goose_model_parser_and_env_openrouter(monkeypatch):
    import goose_client

    monkeypatch.setattr("openrouter_client.api_key", lambda: "sk-or-test")
    monkeypatch.setattr("openrouter_client.base_url", lambda: "https://openrouter.ai/api/v1")

    provider, model = goose_client.split_goose_model("goose:openrouter:z-ai/glm-5.2")
    assert provider == "openrouter"
    assert model == "z-ai/glm-5.2"

    env = goose_client._goose_env("goose:openrouter:z-ai/glm-5.2", "default")
    assert env["GOOSE_PROVIDER"] == "openrouter"
    assert env["GOOSE_MODEL"] == "z-ai/glm-5.2"
    assert env["GOOSE_MODE"] == "approve"
    assert env["OPENROUTER_API_KEY"] == "sk-or-test"


def test_goose_auto_selector_sets_mode_without_changing_provider(monkeypatch):
    import goose_client

    monkeypatch.setattr("openrouter_client.api_key", lambda: "sk-or-test")
    monkeypatch.setattr("openrouter_client.base_url", lambda: "https://openrouter.ai/api/v1")

    provider, model = goose_client.split_goose_model("goose:auto:openrouter:z-ai/glm-5.2")
    assert provider == "openrouter"
    assert model == "z-ai/glm-5.2"
    assert goose_client.permission_mode_for_model("goose:auto:openrouter:z-ai/glm-5.2", "approve") == "auto"

    env = goose_client._goose_env("goose:auto:openrouter:z-ai/glm-5.2", "auto")
    assert env["GOOSE_PROVIDER"] == "openrouter"
    assert env["GOOSE_MODEL"] == "z-ai/glm-5.2"
    assert env["GOOSE_MODE"] == "auto"


def test_goose_model_parser_and_env_dream(monkeypatch):
    import goose_client

    class Cfg:
        dream_host = "http://localhost:8787"
        dream_context_size = 131072

    monkeypatch.setattr("config.config", Cfg())
    env = goose_client._goose_env("goose:dream:diffusiongemma-test", "chat")
    assert env["GOOSE_PROVIDER"] == "openai"
    assert env["GOOSE_MODEL"] == "diffusiongemma-test"
    assert env["GOOSE_MODE"] == "chat"
    assert env["OPENAI_HOST"] == "http://127.0.0.1:8787"
    assert env["OPENAI_BASE_PATH"] == "v1/chat/completions"
    assert env["GOOSE_CONTEXT_LIMIT"] == "131072"


def test_goose_dispatch_normalizes_acp_updates():
    import goose_client

    state = {}
    assert goose_client.dispatch_session_update(
        {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}},
        state,
    ) == [{"type": "text_delta", "text": "hi"}]

    events = goose_client.dispatch_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "t1",
            "title": "Read",
            "content": {"path": "README.md"},
        },
        state,
    )
    assert events[0] == {"type": "tool_start", "name": "Read", "tool_id": "t1"}
    assert events[1]["type"] == "tool_input_delta"

    events = goose_client.dispatch_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "t1",
            "status": "completed",
            "content": {"text": "done"},
        },
        state,
    )
    assert events == [{"type": "tool_result", "content": "done", "tool_id": "t1", "is_error": False}]


def test_server_goose_models_are_in_main_picker():
    import server

    groups = {group["group"]: group["models"] for group in server.CC_MODELS}
    assert "Goose ACP" in groups
    values = {m["value"] for m in groups["Goose ACP"]}
    assert "goose:openrouter:z-ai/glm-5.2" in values
    assert any(v.startswith("goose:dream:") for v in values)
    auto_values = {m["value"] for m in groups["Goose ACP - Auto/Subagents"]}
    assert "goose:auto:openrouter:z-ai/glm-5.2" in auto_values
    assert any(v.startswith("goose:auto:dream:") for v in auto_values)
    assert server._mode_for_cc_model("goose:openrouter:z-ai/glm-5.2") == "goose"
    assert server._mode_for_cc_model("goose:auto:openrouter:z-ai/glm-5.2") == "goose"


@pytest.mark.asyncio
async def test_goose_resume_walk_skips_foreign_mode_session(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation("Goose Isolation", mode="goose", project_dir=str(tmp_path))
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(conv["id"], "assistant", "dream reply", parent_id=u1["id"], cc_session_id="dream-sess")
    await db.update_message_content(a1["id"], cc_session_mode="dream")
    u2 = await db.add_message(conv["id"], "user", "second", parent_id=a1["id"])

    captured = {}

    async def fake_run_goose(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["resume_session_id"] = kwargs.get("resume_session_id")

        class Proc:
            pid = 4444
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-new"}
            yield {"type": "text_delta", "text": "ok"}
            yield {"type": "result", "session_id": "goose-new"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    assert captured["resume_session_id"] is None
    assert "first" in captured["prompt"]
    assert "second" in captured["prompt"]


@pytest.mark.asyncio
async def test_goose_resume_walk_uses_same_mode_session(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation("Goose Resume", mode="goose", project_dir=str(tmp_path))
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(conv["id"], "assistant", "goose reply", parent_id=u1["id"], cc_session_id="goose-sess")
    await db.update_message_content(a1["id"], cc_session_mode="goose")
    u2 = await db.add_message(conv["id"], "user", "second", parent_id=a1["id"])

    captured = {}

    async def fake_run_goose(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["resume_session_id"] = kwargs.get("resume_session_id")
        captured["fork_session"] = kwargs.get("fork_session")

        class Proc:
            pid = 4445
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-fork"}
            yield {"type": "text_delta", "text": "continued"}
            yield {"type": "result", "session_id": "goose-fork"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    assert captured["resume_session_id"] == "goose-sess"
    assert captured["fork_session"] is True
    assert captured["prompt"] == "second"


@pytest.mark.asyncio
async def test_goose_auto_selector_overrides_conservative_permission_mode(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation(
        "Goose Auto",
        mode="goose",
        project_dir=str(tmp_path),
    )
    await db.update_conversation_fields(
        conv["id"],
        cc_model="goose:auto:openrouter:z-ai/glm-5.2",
        cc_permission_mode="approve",
    )
    u1 = await db.add_message(conv["id"], "user", "delegate this")

    captured = {}

    async def fake_run_goose(prompt, **kwargs):
        captured["model"] = kwargs.get("model")
        captured["permission_mode"] = kwargs.get("permission_mode")

        class Proc:
            pid = 4446
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-auto"}
            yield {"type": "text_delta", "text": "auto"}
            yield {"type": "result", "session_id": "goose-auto"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u1["id"], "cc_permission_mode": "approve"},
    )

    assert captured["model"] == "goose:auto:openrouter:z-ai/glm-5.2"
    assert captured["permission_mode"] == "auto"
