"""Goose ACP integration invariants."""

import asyncio
import json

import pytest


async def test_goose_permission_bridge_forwards_generation_scope(monkeypatch):
    import goose_client

    captured = {}

    class FakeResponse:
        def json(self):
            return {"allow": True}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, *, json, timeout):
            captured.update(json)
            return FakeResponse()

    class FakeRpc:
        async def respond(self, req_id, *, result):
            captured["reply"] = result

    monkeypatch.setattr(goose_client.httpx, "AsyncClient", FakeClient)
    await goose_client._bridge_permission(
        FakeRpc(),
        9,
        {
            "toolCall": {"title": "shell"},
            "options": [{"id": "allow_once", "label": "Allow once"}],
        },
        7,
        3000,
        "gen:41",
    )

    assert captured["permission_scope"] == "gen:41"
    assert captured["reply"]["outcome"]["optionId"] == "allow_once"


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
    assert env["OPENROUTER_HOST"] == "https://openrouter.ai"
    assert env["GOOSE_PROVIDER__TYPE"] == "openrouter"
    assert env["GOOSE_PROVIDER__API_KEY"] == "sk-or-test"
    assert env["GOOSE_PROVIDER__HOST"] == "https://openrouter.ai"


def test_goose_env_openrouter_merges_reasoning_effort(monkeypatch):
    import goose_client

    monkeypatch.setenv("OPENROUTER_PARAMETERS", '{"plugins":[{"id":"web"}]}')
    monkeypatch.setattr("openrouter_client.api_key", lambda: "sk-or-test")
    monkeypatch.setattr("openrouter_client.base_url", lambda: "https://openrouter.ai/api/v1")

    env = goose_client._goose_env("goose:openrouter:deepseek/deepseek-v4-flash-0731", "approve", "max")
    params = json.loads(env["OPENROUTER_PARAMETERS"])

    assert params["plugins"] == [{"id": "web"}]
    assert params["reasoning"] == {"effort": "max", "enabled": True}


def test_goose_prompt_blocks_include_native_images(tmp_path):
    import base64
    import goose_client

    image = tmp_path / "sample.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nsample")

    blocks = goose_client._prompt_blocks("inspect this", [image])

    assert blocks[0] == {"type": "text", "text": "inspect this"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["mimeType"] == "image/png"
    assert blocks[1]["data"] == base64.b64encode(image.read_bytes()).decode("ascii")
    assert blocks[1]["uri"].startswith("file:///")


def test_goose_windows_app_path_resolves_to_embedded_cli(monkeypatch, tmp_path):
    import goose_client

    install = tmp_path / "Goose"
    app_exe = install / "Goose.exe"
    cli_exe = install / "resources" / "bin" / "goose.exe"
    cli_exe.parent.mkdir(parents=True)
    app_exe.write_text("", encoding="utf-8")
    cli_exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(goose_client.sys, "platform", "win32")
    monkeypatch.setenv("GOOSE_EXE", str(app_exe))

    assert goose_client.default_goose_exe() == str(cli_exe)
    assert goose_client._goose_command(str(app_exe)) == [str(cli_exe)]


def test_goose_acp_work_dir_is_absolute(monkeypatch, tmp_path):
    import goose_client

    monkeypatch.chdir(tmp_path)
    assert goose_client._goose_work_dir(".") == str(tmp_path.resolve())


def test_goose_auto_selector_sets_mode_without_changing_provider(monkeypatch):
    import goose_client

    monkeypatch.setattr("openrouter_client.api_key", lambda: "sk-or-test")
    monkeypatch.setattr("openrouter_client.base_url", lambda: "https://openrouter.ai/api/v1")

    provider, model = goose_client.split_goose_model("goose:auto:openrouter:z-ai/glm-5.2")
    assert provider == "openrouter"
    assert model == "z-ai/glm-5.2"
    assert goose_client.permission_mode_for_model("goose:auto:openrouter:z-ai/glm-5.2", "approve") == "smart_approve"

    env = goose_client._goose_env("goose:auto:openrouter:z-ai/glm-5.2", "smart_approve")
    assert env["GOOSE_PROVIDER"] == "openrouter"
    assert env["GOOSE_MODEL"] == "z-ai/glm-5.2"
    assert env["GOOSE_MODE"] == "smart_approve"
    assert env["OPENROUTER_API_KEY"] == "sk-or-test"
    assert env["OPENROUTER_HOST"] == "https://openrouter.ai"
    assert env["GOOSE_PROVIDER__TYPE"] == "openrouter"
    assert env["GOOSE_PROVIDER__API_KEY"] == "sk-or-test"


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
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:8787"
    assert env["OPENAI_API_KEY"] == "loom-local"
    assert env["OPENAI_BASE_PATH"] == "v1/chat/completions"
    assert env["GOOSE_PROVIDER__TYPE"] == "openai"
    assert env["GOOSE_PROVIDER__HOST"] == "http://127.0.0.1:8787"
    assert env["GOOSE_PROVIDER__API_KEY"] == "loom-local"
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


@pytest.mark.asyncio
async def test_goose_rpc_timeout_names_method():
    import asyncio
    import goose_client

    class Stdin:
        def write(self, data):
            pass

        async def drain(self):
            pass

    class Stdout:
        async def readline(self):
            await asyncio.sleep(1)
            return b""

    class Proc:
        stdin = Stdin()
        stdout = Stdout()

    proc = Proc()
    rpc = goose_client._RpcConn(proc)

    with pytest.raises(asyncio.TimeoutError, match="goose initialize timed out after"):
        await goose_client._rpc_request_via_reader(rpc, "initialize", {}, proc, {}, timeout=0.01)


def test_server_goose_models_are_in_main_picker():
    import server

    groups = {group["group"]: group["models"] for group in server.CC_MODELS}
    assert "Goose ACP" in groups
    values = {m["value"] for m in groups["Goose ACP"]}
    assert "goose:openrouter:z-ai/glm-5.2" in values
    assert any(v.startswith("goose:dream:") for v in values)
    auto_values = {m["value"] for m in groups["Goose ACP - Smart/Subagents"]}
    assert "goose:auto:openrouter:z-ai/glm-5.2" in auto_values
    assert any(v.startswith("goose:auto:dream:") for v in auto_values)
    assert server._mode_for_cc_model("goose:openrouter:z-ai/glm-5.2") == "goose"
    assert server._mode_for_cc_model("goose:auto:openrouter:z-ai/glm-5.2") == "goose"


def test_goose_model_context_unwraps_provider_thresholds():
    import model_context

    assert model_context.handoff_threshold("goose:openrouter:z-ai/glm-5.2") == model_context.THRESHOLD_OPENROUTER_GLM_52
    assert model_context.handoff_threshold("goose:auto:openrouter:moonshotai/kimi-k2.7-code") == model_context.THRESHOLD_OPENROUTER_KIMI_K27_CODE
    assert not model_context.needs_handoff("goose:openrouter:z-ai/glm-5.2", 200_000)
    assert model_context.needs_handoff("goose:auto:openrouter:moonshotai/kimi-k2.7-code", 300_000)


def test_goose_selector_wraps_openrouter_and_local_models():
    import server

    assert (
        server._goose_selector_for_model("openrouter:moonshotai/kimi-k2.7-code")
        == "goose:openrouter:moonshotai/kimi-k2.7-code"
    )
    assert (
        server._goose_selector_for_model("moonshotai/kimi-k2.7-code")
        == "goose:openrouter:moonshotai/kimi-k2.7-code"
    )
    assert (
        server._goose_selector_for_model("Qwen3.8-27B-NVFP4-MTP-Q8attn.gguf")
        == "goose:llama:Qwen3.8-27B-NVFP4-MTP-Q8attn.gguf"
    )


@pytest.mark.asyncio
async def test_goose_conversation_update_keeps_openrouter_inside_goose(tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation("Goose Model Save", mode="goose", project_dir=str(tmp_path))

    updated = await server.api_update_conversation(
        conv["id"],
        {"cc_model": "openrouter:moonshotai/kimi-k2.7-code"},
    )

    assert updated["mode"] == "goose"
    assert updated["cc_model"] == "goose:openrouter:moonshotai/kimi-k2.7-code"


@pytest.mark.asyncio
async def test_goose_generation_wraps_local_dropdown_model(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation("Goose Local Save", mode="goose", project_dir=str(tmp_path))
    captured = {}

    async def fake_goose_generation(websocket, conv_id, conv, data):
        captured["mode"] = conv["mode"]
        captured["cc_model"] = data.get("cc_model")

    async def fail_claude_generation(*args, **kwargs):
        raise AssertionError("bare local model should stay routed through Goose")

    monkeypatch.setattr(server, "_handle_goose_generation", fake_goose_generation)
    monkeypatch.setattr(server, "_handle_claude_generation", fail_claude_generation)

    await server._handle_generation(
        None,
        conv["id"],
        {"action": "generate", "cc_model": "Qwen3.8-27B-NVFP4-MTP-Q8attn.gguf"},
    )

    assert captured == {
        "mode": "goose",
        "cc_model": "goose:llama:Qwen3.8-27B-NVFP4-MTP-Q8attn.gguf",
    }


@pytest.mark.asyncio
async def test_goose_resume_walk_skips_foreign_mode_session(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation("Goose Isolation", mode="goose", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
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
async def test_goose_switch_back_replays_intervening_provider_turn(
    monkeypatch, tmp_database, tmp_path
):
    import database as db
    import server

    conv = await db.create_conversation(
        "Goose A-B-A", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(
        conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2"
    )
    u1 = await db.add_message(conv["id"], "user", "first")
    g1 = await db.add_message(
        conv["id"], "assistant", "old goose reply", parent_id=u1["id"],
        cc_session_id="goose-old",
    )
    await db.update_message_content(
        g1["id"], cc_session_mode="goose",
        cc_model_used="goose:openrouter:z-ai/glm-5.2",
    )
    u2 = await db.add_message(conv["id"], "user", "ask codex", parent_id=g1["id"])
    c1 = await db.add_message(
        conv["id"], "assistant", "intervening codex reply", parent_id=u2["id"],
        cc_session_id="codex-middle",
    )
    await db.update_message_content(
        c1["id"], cc_session_mode="codex", cc_model_used="gpt-5.6-sol"
    )
    u3 = await db.add_message(
        conv["id"], "user", "back to goose", parent_id=c1["id"]
    )

    captured = {}

    async def fake_run_goose(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["resume_session_id"] = kwargs.get("resume_session_id")

        class Proc:
            pid = 4457
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-new"}
            yield {"type": "text_delta", "text": "replayed"}
            yield {"type": "result", "session_id": "goose-new"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u3["id"]},
    )

    assert captured["resume_session_id"] is None
    assert "old goose reply" in captured["prompt"]
    assert "intervening codex reply" in captured["prompt"]
    assert "back to goose" in captured["prompt"]


@pytest.mark.asyncio
async def test_goose_resume_walk_uses_same_mode_session(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation("Goose Resume", mode="goose", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(conv["id"], "assistant", "goose reply", parent_id=u1["id"], cc_session_id="goose-sess")
    await db.update_message_content(
        a1["id"],
        cc_session_mode="goose",
        cc_model_used="goose:openrouter:z-ai/glm-5.2",
    )
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
async def test_goose_resume_walk_skips_different_goose_model_session(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation("Goose Provider Isolation", mode="goose", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(
        conv["id"],
        "assistant",
        "dream reply",
        parent_id=u1["id"],
        cc_session_id="goose-dream-sess",
    )
    await db.update_message_content(
        a1["id"],
        cc_session_mode="goose",
        cc_model_used="goose:dream:diffusiongemma-26b-a4b-it-nvfp4",
    )
    u2 = await db.add_message(conv["id"], "user", "second", parent_id=a1["id"])

    captured = {}

    async def fake_run_goose(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["resume_session_id"] = kwargs.get("resume_session_id")
        captured["model"] = kwargs.get("model")

        class Proc:
            pid = 4456
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-openrouter-new"}
            yield {"type": "text_delta", "text": "rebuilt on openrouter"}
            yield {"type": "result", "session_id": "goose-openrouter-new"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    assert captured["resume_session_id"] is None
    assert captured["model"] == "goose:openrouter:z-ai/glm-5.2"
    assert "first" in captured["prompt"]
    assert "second" in captured["prompt"]


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
    assert captured["permission_mode"] == "smart_approve"


@pytest.mark.asyncio
async def test_goose_generation_passes_selected_effort(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation("Goose Effort", mode="goose", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:deepseek/deepseek-v4-flash-0731")
    u1 = await db.add_message(conv["id"], "user", "think hard")

    captured = {}

    async def fake_run_goose(prompt, **kwargs):
        captured["effort"] = kwargs.get("effort")

        class Proc:
            pid = 4449
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-effort"}
            yield {"type": "text_delta", "text": "effort ok"}
            yield {"type": "result", "session_id": "goose-effort"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u1["id"], "cc_effort": "max"},
    )

    assert captured["effort"] == "max"
    saved = await db.get_conversation(conv["id"])
    assert saved["cc_effort"] == "max"


@pytest.mark.asyncio
async def test_goose_rejects_ambiguous_model_before_launch(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation("Goose Invalid Selector", mode="goose", project_dir=str(tmp_path))
    sent = []

    async def capture(_conv_id, event):
        sent.append(event)

    async def fail_launch(*_args, **_kwargs):
        raise AssertionError("Goose must not launch an implicitly-routed model")

    monkeypatch.setattr(server, "_ws_send", capture)
    monkeypatch.setattr(server.goose_client, "run_goose", fail_launch)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "cc_model": "not-a-provider-selector"},
    )

    error = next(event for event in sent if event.get("type") == "error")
    assert error["provider"] == "goose"
    assert error["stage"] == "model_resolution"
    assert error["requested_model"] == "not-a-provider-selector"
    assert "not a Goose, OpenRouter, loaded GGUF, or Dream model" in error["error"]


@pytest.mark.asyncio
async def test_goose_resume_turn_passes_latest_image_attachment(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    image = tmp_path / "sample.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nsample")

    conv = await db.create_conversation("Goose Image", mode="goose", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(conv["id"], "assistant", "prior", parent_id=u1["id"], cc_session_id="goose-image-session")
    await db.update_message_content(
        a1["id"],
        cc_session_mode="goose",
        cc_model_used="goose:openrouter:z-ai/glm-5.2",
    )
    u2 = await db.add_message(
        conv["id"],
        "user",
        "what is in this image?",
        parent_id=a1["id"],
        image_path=json.dumps([str(image)]),
    )

    captured = {}

    async def fake_run_goose(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["image_paths"] = kwargs.get("image_paths")
        captured["resume_session_id"] = kwargs.get("resume_session_id")

        class Proc:
            pid = 4457
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-image-fork"}
            yield {"type": "text_delta", "text": "image ok"}
            yield {"type": "result", "session_id": "goose-image-fork"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    copied = tmp_path / "attached_files" / "sample.png"
    assert captured["resume_session_id"] == "goose-image-session"
    assert "sample.png (in attached_files/)" in captured["prompt"]
    assert captured["image_paths"] == [copied]
    assert copied.read_bytes() == image.read_bytes()


@pytest.mark.asyncio
async def test_goose_finalizes_content_and_usage_tokens(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation("Goose Tokens", mode="goose", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "count tokens")

    async def fake_run_goose(prompt, **kwargs):
        class Proc:
            pid = 4447
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-token"}
            yield {"type": "text_delta", "text": "token ok"}
            yield {"type": "usage", "input_tokens": 11, "output_tokens": 5}
            yield {"type": "result", "session_id": "goose-token"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u1["id"]},
    )

    leaf = await db.get_active_leaf(conv["id"])
    assert leaf["content"] == "token ok"
    assert leaf["turn_input_tokens"] == 11
    assert leaf["turn_output_tokens"] == 5
    assert leaf["cc_session_id"] == "goose-token"
    assert leaf["cc_session_mode"] == "goose"


@pytest.mark.asyncio
async def test_goose_retries_when_resume_stream_raises(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation("Goose Bad Resume", mode="goose", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(conv["id"], "assistant", "goose reply", parent_id=u1["id"], cc_session_id="goose-stale")
    await db.update_message_content(
        a1["id"], cc_session_mode="goose",
        cc_model_used="goose:openrouter:z-ai/glm-5.2",
    )
    u2 = await db.add_message(conv["id"], "user", "second", parent_id=a1["id"])

    calls = []

    async def fake_run_goose(prompt, **kwargs):
        calls.append({"prompt": prompt, "resume_session_id": kwargs.get("resume_session_id")})

        class Proc:
            pid = 4448 + len(calls)
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            if kwargs.get("resume_session_id"):
                raise RuntimeError("bad goose session")
            yield {"type": "session_info", "session_id": "goose-rebuilt"}
            yield {"type": "text_delta", "text": "rebuilt"}
            yield {"type": "result", "session_id": "goose-rebuilt"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    assert [c["resume_session_id"] for c in calls] == ["goose-stale", None]
    assert calls[0]["prompt"] == "second"
    assert "first" in calls[1]["prompt"]
    assert "second" in calls[1]["prompt"]
    leaf = await db.get_active_leaf(conv["id"])
    assert leaf["content"] == "rebuilt"
    assert leaf["cc_session_id"] == "goose-rebuilt"


@pytest.mark.asyncio
async def test_goose_retries_empty_resumed_turn(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation("Goose Empty Resume", mode="goose", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(conv["id"], "assistant", "goose reply", parent_id=u1["id"], cc_session_id="goose-empty")
    await db.update_message_content(
        a1["id"], cc_session_mode="goose",
        cc_model_used="goose:openrouter:z-ai/glm-5.2",
    )
    u2 = await db.add_message(conv["id"], "user", "second", parent_id=a1["id"])

    calls = []

    async def fake_run_goose(prompt, **kwargs):
        calls.append(kwargs.get("resume_session_id"))

        class Proc:
            pid = 4451 + len(calls)
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            if kwargs.get("resume_session_id"):
                yield {"type": "session_info", "session_id": "goose-empty-fork"}
                yield {"type": "result", "session_id": "goose-empty-fork"}
                return
            yield {"type": "session_info", "session_id": "goose-retry"}
            yield {"type": "text_delta", "text": "fallback text"}
            yield {"type": "result", "session_id": "goose-retry"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    assert calls == ["goose-empty", None]
    leaf = await db.get_active_leaf(conv["id"])
    assert leaf["content"] == "fallback text"
    assert leaf["cc_session_id"] == "goose-retry"


@pytest.mark.asyncio
async def test_goose_updates_live_snapshot_and_merges_thinking(monkeypatch, tmp_database, tmp_path):
    import asyncio
    import database as db
    import server

    conv = await db.create_conversation("Goose Snapshot", mode="goose", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "think")
    gen_key = (conv["id"], u1["id"], 9001)
    asyncio.current_task()._gen_key = gen_key
    snapshots = []
    original_update = server._update_gen_snapshot

    def capture_snapshot(key, **fields):
        snapshots.append((key, dict(fields)))
        return original_update(key, **fields)

    async def fake_run_goose(prompt, **kwargs):
        class Proc:
            pid = 4454
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-snap"}
            yield {"type": "thinking_delta", "text": "alpha "}
            yield {"type": "thinking_delta", "text": "beta"}
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "result", "session_id": "goose-snap"}

        return Proc(), events()

    monkeypatch.setattr(server, "_update_gen_snapshot", capture_snapshot)
    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    try:
        await server._handle_goose_generation(
            websocket=None,
            conv_id=conv["id"],
            conv=await db.get_conversation(conv["id"]),
            data={"action": "generate", "parent_id": u1["id"]},
        )
    finally:
        delattr(asyncio.current_task(), "_gen_key")

    assert any(key == gen_key and fields.get("full_text") == "done" for key, fields in snapshots)
    leaf = await db.get_active_leaf(conv["id"])
    blocks = json.loads(leaf["content_blocks"])
    assert blocks == [
        {"type": "thinking", "text": "alpha beta"},
        {"type": "text", "text": "done"},
    ]


@pytest.mark.asyncio
async def test_goose_generation_does_not_persist_blank_exception(monkeypatch, tmp_database, tmp_path):
    import asyncio
    import database as db
    import server

    conv = await db.create_conversation("Goose Blank Error", mode="goose", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "use openrouter")

    async def fake_run_goose(prompt, **kwargs):
        class Proc:
            pid = 4455
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            raise asyncio.TimeoutError()
            yield

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u1["id"]},
    )

    leaf = await db.get_active_leaf(conv["id"])
    assert leaf["content"] == "[Error: Goose operation timed out]"


@pytest.mark.asyncio
async def test_goose_generation_does_not_persist_blank_error_event(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation("Goose Blank Event", mode="goose", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "use openrouter")

    async def fake_run_goose(prompt, **kwargs):
        class Proc:
            pid = 4456
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-blank"}
            yield {"type": "error", "error": "   "}
            yield {"type": "result", "session_id": "goose-blank", "stop_reason": "error"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u1["id"]},
    )

    leaf = await db.get_active_leaf(conv["id"])
    assert leaf["content"] == "[Error: Goose error]"
