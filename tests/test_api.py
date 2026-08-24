"""Tests for REST API endpoints."""

import json

import pytest

import database as db


class _FakeAnthropicResponse:
    def __init__(self, status_code, data=None, text=""):
        self.status_code = status_code
        self._data = data or {}
        self.text = text

    def json(self):
        return self._data


class _FakeAnthropicClient:
    response = _FakeAnthropicResponse(200)
    last_request = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, params=None, headers=None):
        type(self).last_request = {
            "url": url,
            "params": params,
            "headers": headers,
        }
        return type(self).response


async def test_health_endpoint(client, mock_llama):
    """GET /api/health returns status info."""
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "models" in data


async def test_ui_entrypoint_and_executable_assets_are_revalidated(client):
    index_resp = await client.get("/")
    chat_resp = await client.get("/static/chat.js")

    assert index_resp.status_code == 200
    assert chat_resp.status_code == 200
    assert "no-cache" in index_resp.headers.get("cache-control", "")
    assert "no-cache" in chat_resp.headers.get("cache-control", "")


async def test_local_models_endpoint(client, mock_llama):
    """GET /api/local/models returns the backend-aware model cache.

    (Replaced /api/local/models when model listing became cache-based.)"""
    resp = await client.get("/api/local/models")
    assert resp.status_code == 200
    data = resp.json()
    assert "models" in data


async def test_create_weave_conversation(client, mock_llama):
    """POST /api/conversations with weave mode."""
    resp = await client.post("/api/conversations", json={
        "title": "Weave Test",
        "mode": "weave",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Weave Test"
    assert data["mode"] == "weave"


async def test_create_system_only_weave_conversation(client, mock_llama):
    """POST /api/conversations can create a minimal system-message-only Weave space."""
    resp = await client.post("/api/conversations", json={
        "title": "Minimal Weave Test",
        "mode": "weave",
        "character_id": "should-be-ignored",
        "persona_id": "also-ignored",
        "lore_ids": ["ignored"],
        "first_turn": "character",
        "system_only": True,
        "system_prompt": "Use only branch history and this instruction.",
        "local_model": "diffusiongemma-26b-a4b-it-nvfp4",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "weave"
    assert data["character_id"] is None
    assert data["persona_id"] is None
    assert data["lore_ids"] == "[]"
    assert data["system_only"] == 1
    assert data["minimal_ooda_enabled"] == 1
    assert data["system_prompt"] == "Use only branch history and this instruction."
    assert data["ooda_enabled"] == 0
    assert data["local_model"] == "diffusiongemma-26b-a4b-it-nvfp4"


def test_minimal_weave_helpers_do_not_reintroduce_defaults():
    import server

    assert server._truthy_setting("1") is True
    assert server._truthy_setting("true") is True
    assert server._truthy_setting("0") is False
    assert server._truthy_setting("false") is False
    minimal_prompt = server._minimal_weave_system_prompt({"system_prompt": ""})
    assert "OODA" in minimal_prompt
    assert "<ooda>" not in minimal_prompt
    assert "read_state" not in minimal_prompt
    plain_prompt = server._minimal_weave_system_prompt({
        "system_prompt": "ONLY THIS",
        "minimal_ooda_enabled": 0,
    })
    assert plain_prompt == "ONLY THIS"


async def test_cc_models_hide_deprecated_umans_by_default(client, monkeypatch):
    import server

    monkeypatch.setattr(server.config, "enable_umans_models", False)
    resp = await client.get("/api/cc-models")
    assert resp.status_code == 200
    groups = resp.json()
    assert all("umans" not in group["group"].lower() for group in groups)


async def test_cc_models_includes_new_openrouter_models(client):
    import server

    resp = await client.get("/api/cc-models")
    assert resp.status_code == 200
    groups = {g["group"]: g["models"] for g in resp.json()}
    openrouter = {m["value"] for m in groups.get("OpenRouter", [])}
    assert "openrouter:openai/gpt-5.6-luna" in openrouter
    assert "openrouter:deepseek/deepseek-v4-flash-0731" in openrouter


async def test_create_umans_conversation_blocked_when_disabled(client, monkeypatch):
    import server

    monkeypatch.setattr(server.config, "enable_umans_models", False)
    resp = await client.post("/api/conversations", json={
        "title": "Deprecated Umans",
        "mode": "claude",
        "cc_model": "umans-coder",
    })
    assert resp.status_code == 400
    assert "deprecated" in resp.json()["detail"].lower()


async def test_config_get_reloads_disk_before_returning(client, tmp_path, monkeypatch):
    import server

    exe = tmp_path / "llama-server.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(server.config, "llama_server_exe", "llama-server")
    monkeypatch.setattr(server.config, "save", lambda: None)

    def fake_load():
        server.config.llama_server_exe = str(exe)

    monkeypatch.setattr(server.config, "load", fake_load)

    resp = await client.get("/api/config")

    assert resp.status_code == 200
    assert resp.json()["llama_server_exe"] == str(exe)


async def test_config_update_preserves_valid_llama_path_from_stale_ui(client, tmp_path, monkeypatch):
    import server

    exe = tmp_path / "llama-server.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(server.config, "llama_server_exe", str(exe))
    monkeypatch.setattr(server.config, "llama_models_dir", str(tmp_path / "models"))
    monkeypatch.setattr(server.config, "load", lambda: None)
    monkeypatch.setattr(server.config, "save", lambda: None)

    resp = await client.put("/api/config", json={"llama_server_exe": "llama-server"})

    assert resp.status_code == 200
    assert resp.json()["llama_server_exe"] == str(exe)


async def test_openrouter_secret_endpoint_writes_dotenv_without_echoing_key(client, tmp_path, monkeypatch):
    import server

    env_path = tmp_path / ".env"
    env_path.write_text("OTHER_SETTING=kept\nOPENROUTER_API_KEY=old-value\n", encoding="utf-8")
    monkeypatch.setattr(server.openrouter_client, "_dotenv_path", lambda: env_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MANAGEMENT_KEY", raising=False)

    api_key = "sk-or-v1-test-inference-abcdef1234"
    management_key = "sk-or-v1-test-management-fedcba9876"
    resp = await client.post("/api/openrouter/secrets", json={
        "api_key": api_key,
        "management_key": management_key,
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert api_key not in str(data)
    assert management_key not in str(data)
    assert data["status"]["api_key"]["preview"] == "sk-or-v...1234"
    assert data["status"]["api_key"]["source"] == ".env"

    saved = env_path.read_text(encoding="utf-8")
    assert "OTHER_SETTING=kept" in saved
    assert f"OPENROUTER_API_KEY={api_key}" in saved
    assert f"OPENROUTER_MANAGEMENT_KEY={management_key}" in saved
    assert saved.count("OPENROUTER_API_KEY=") == 1


async def test_openrouter_secret_endpoint_preserves_omitted_keys(client, tmp_path, monkeypatch):
    import server

    env_path = tmp_path / ".env"
    env_path.write_text(
        "OPENROUTER_API_KEY=old-inference\nOPENROUTER_MANAGEMENT_KEY=old-management\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server.openrouter_client, "_dotenv_path", lambda: env_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MANAGEMENT_KEY", raising=False)

    resp = await client.post("/api/openrouter/secrets", json={
        "management_key": "new-management",
    })

    assert resp.status_code == 200
    saved = env_path.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=old-inference" in saved
    assert "OPENROUTER_MANAGEMENT_KEY=new-management" in saved


async def test_openrouter_secret_endpoint_clears_explicit_empty_key(client, tmp_path, monkeypatch):
    import server

    env_path = tmp_path / ".env"
    env_path.write_text(
        "OPENROUTER_API_KEY=old-inference\nOPENROUTER_MANAGEMENT_KEY=old-management\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server.openrouter_client, "_dotenv_path", lambda: env_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MANAGEMENT_KEY", raising=False)

    resp = await client.post("/api/openrouter/secrets", json={
        "api_key": "",
    })

    assert resp.status_code == 200
    saved = env_path.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=" not in saved
    assert "OPENROUTER_MANAGEMENT_KEY=old-management" in saved


async def test_system_only_weave_generation_uses_only_system_prompt(monkeypatch):
    """Minimal Weave sends the stored system prompt plus prompt-only OODA."""
    import server

    conv = await db.create_conversation(
        "Dirty Minimal Weave",
        character_id="leaky-character",
        mode="weave",
    )
    await db.update_conversation_fields(
        conv["id"],
        persona_id="leaky-persona",
        lore_ids=json.dumps(["leaky-lore"]),
        custom_scene="LEAKY SCENE",
        system_only=1,
        minimal_ooda_enabled=1,
        system_prompt="ONLY THIS SYSTEM MESSAGE",
        ooda_enabled=1,
        local_model="Qwen3.6-27B-NVFP4.gguf",
    )
    user_msg = await db.add_message(conv["id"], "user", "Start here.")
    conv = await db.get_conversation(conv["id"])

    loader_calls = []

    def record_loader(name):
        def _loader(*args, **kwargs):
            loader_calls.append(name)
            return {
                "name": name,
                "personality": f"LEAKY {name} PERSONALITY",
                "scenario": f"LEAKY {name} SCENARIO",
                "content": f"LEAKY {name} CONTENT",
                "example_messages": [
                    {"role": "assistant", "content": f"LEAKY {name} EXAMPLE"},
                ],
            }
        return _loader

    captured = {}

    async def fake_stream_chat(messages, model=None, **kwargs):
        captured["messages"] = messages
        captured["model"] = model
        captured["max_tokens"] = kwargs.get("max_tokens")
        yield "ok"

    async def fake_update_rolling_summary(conv_id):
        return None

    monkeypatch.setattr(server, "load_character", record_loader("character"))
    monkeypatch.setattr(server, "load_persona", record_loader("persona"))
    monkeypatch.setattr(server, "load_lore_entry", record_loader("lore"))
    monkeypatch.setattr(server, "stream_chat", fake_stream_chat)
    monkeypatch.setattr("context_manager.update_rolling_summary", fake_update_rolling_summary)

    await server._handle_weave_generation(
        None,
        conv["id"],
        conv,
        {"action": "generate", "parent_id": user_msg["id"]},
    )

    assert loader_calls == []
    assert captured["model"] == "Qwen3.6-27B-NVFP4.gguf"
    assert captured["max_tokens"]
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][0]["content"].startswith("ONLY THIS SYSTEM MESSAGE\n\n")
    assert "OODA" in captured["messages"][0]["content"]
    assert "<ooda>" not in captured["messages"][0]["content"]
    assert "read_state" not in captured["messages"][0]["content"]
    assert captured["messages"][1:] == [
        {"role": "user", "content": "Start here."},
    ]
    prompt_text = "\n".join(m["content"] for m in captured["messages"])
    assert "collaborative fiction writer" not in prompt_text
    assert "LEAKY" not in prompt_text
    assert "My character" not in prompt_text
    assert "Background" not in prompt_text


async def test_weave_generation_persists_streamed_usage(monkeypatch):
    import server

    conv = await db.create_conversation(
        "Weave Usage",
        mode="weave",
    )
    await db.update_conversation_fields(
        conv["id"],
        system_only=1,
        system_prompt="ONLY THIS SYSTEM MESSAGE",
        local_model="diffusion-gemma-test",
    )
    user_msg = await db.add_message(conv["id"], "user", "Start here.")
    conv = await db.get_conversation(conv["id"])

    async def fake_stream_chat(messages, model=None, **kwargs):
        yield "ok"
        yield {"type": "usage", "input_tokens": 123, "output_tokens": 45}

    async def fake_update_rolling_summary(conv_id):
        return None

    monkeypatch.setattr(server, "stream_chat", fake_stream_chat)
    monkeypatch.setattr("context_manager.update_rolling_summary", fake_update_rolling_summary)

    await server._handle_weave_generation(
        None,
        conv["id"],
        conv,
        {"action": "generate", "parent_id": user_msg["id"]},
    )

    children = await db.get_children(user_msg["id"])
    assistant = next(m for m in children if m["role"] == "assistant")
    assert assistant["content"] == "ok"
    assert assistant["turn_input_tokens"] == 123
    assert assistant["turn_output_tokens"] == 45


async def test_weave_generation_persists_thinking_blocks(monkeypatch):
    import server

    conv = await db.create_conversation(
        "Weave Thinking",
        mode="weave",
    )
    await db.update_conversation_fields(
        conv["id"],
        system_only=1,
        system_prompt="ONLY THIS SYSTEM MESSAGE",
        local_model="Qwen3.6-27B-NVFP4.gguf",
    )
    user_msg = await db.add_message(conv["id"], "user", "Start here.")
    conv = await db.get_conversation(conv["id"])

    async def fake_stream_chat(messages, model=None, **kwargs):
        yield {"type": "thinking_delta", "text": "hidden "}
        yield {"type": "thinking_delta", "text": "reasoning"}
        yield "visible answer"

    async def fake_update_rolling_summary(conv_id):
        return None

    monkeypatch.setattr(server, "stream_chat", fake_stream_chat)
    monkeypatch.setattr("context_manager.update_rolling_summary", fake_update_rolling_summary)

    await server._handle_weave_generation(
        None,
        conv["id"],
        conv,
        {"action": "generate", "parent_id": user_msg["id"]},
    )

    children = await db.get_children(user_msg["id"])
    assistant = next(m for m in children if m["role"] == "assistant")
    assert assistant["content"] == "visible answer"
    assert json.loads(assistant["content_blocks"]) == [
        {"type": "thinking", "text": "hidden reasoning"},
        {"type": "text", "text": "visible answer"},
    ]


async def test_system_only_weave_generation_can_disable_minimal_ooda(monkeypatch):
    """Minimal Weave can send only the stored system prompt with no private OODA add-in."""
    import server

    conv = await db.create_conversation(
        "Plain Minimal Weave",
        character_id="leaky-character",
        mode="weave",
    )
    await db.update_conversation_fields(
        conv["id"],
        system_only=1,
        minimal_ooda_enabled=0,
        system_prompt="ONLY THIS SYSTEM MESSAGE",
        local_model="Qwen3.6-27B-NVFP4.gguf",
    )
    user_msg = await db.add_message(conv["id"], "user", "Start here.")
    conv = await db.get_conversation(conv["id"])

    captured = {}

    async def fake_stream_chat(messages, model=None, **kwargs):
        captured["messages"] = messages
        captured["model"] = model
        yield "ok"

    async def fake_update_rolling_summary(conv_id):
        return None

    monkeypatch.setattr(server, "stream_chat", fake_stream_chat)
    monkeypatch.setattr("context_manager.update_rolling_summary", fake_update_rolling_summary)

    await server._handle_weave_generation(
        None,
        conv["id"],
        conv,
        {"action": "generate", "parent_id": user_msg["id"]},
    )

    assert captured["messages"][0] == {
        "role": "system",
        "content": "ONLY THIS SYSTEM MESSAGE",
    }
    assert "OODA" not in "\n".join(m["content"] for m in captured["messages"])


async def test_create_local_conversation(client, mock_llama):
    """POST /api/conversations with mode=local and local_model set."""
    resp = await client.post("/api/conversations", json={
        "title": "Local Test",
        "mode": "local",
        "local_model": "qwen3:4b",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "local"
    assert data["local_model"] == "qwen3:4b"


async def test_create_claude_conversation(client, mock_llama):
    """POST /api/conversations with mode=claude."""
    resp = await client.post("/api/conversations", json={
        "title": "Claude Test",
        "mode": "claude",
        "cc_model": "opus",
        "cc_effort": "high",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "claude"
    assert data["cc_model"] == "opus"


async def test_update_conversation_openrouter_model_sets_openrouter_mode(client, mock_llama):
    conv = await db.create_conversation("OpenRouter Mode", mode="claude")

    resp = await client.put(
        f"/api/conversations/{conv['id']}",
        json={"cc_model": "openrouter:deepseek/deepseek-v4-flash-0731"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "openrouter"
    assert data["cc_model"] == "openrouter:deepseek/deepseek-v4-flash-0731"


async def test_get_conversation_repairs_stale_openrouter_mode(client, mock_llama):
    conv = await db.create_conversation("Stale OpenRouter Mode", mode="codex")
    await db.update_conversation_fields(
        conv["id"],
        cc_model="openrouter:deepseek/deepseek-v4-flash-0731",
    )

    resp = await client.get(f"/api/conversations/{conv['id']}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "openrouter"

    updated = await db.get_conversation(conv["id"])
    assert updated["mode"] == "openrouter"


async def test_codex_goal_endpoint_requires_codex(client, mock_llama):
    conv = await db.create_conversation("Not Codex", mode="weave")

    resp = await client.post(
        f"/api/conversations/{conv['id']}/codex-goal",
        json={"action": "set", "objective": "Finish the migration"},
    )

    assert resp.status_code == 400
    assert "/goal is only available for Codex" in resp.json()["detail"]


async def test_codex_goal_endpoint_rejects_openrouter(client, mock_llama):
    conv = await db.create_conversation("OpenRouter Goal", mode="openrouter")
    await db.update_conversation_fields(
        conv["id"],
        cc_model="openrouter:deepseek/deepseek-v4-flash-0731",
    )

    resp = await client.post(
        f"/api/conversations/{conv['id']}/codex-goal",
        json={"action": "set", "objective": "This should not start Codex"},
    )

    assert resp.status_code == 400
    assert "/goal is only available for Codex" in resp.json()["detail"]


async def test_codex_goal_endpoint_rejects_stale_openrouter_mode(client, mock_llama):
    conv = await db.create_conversation("Stale OpenRouter Goal", mode="codex")
    await db.update_conversation_fields(
        conv["id"],
        cc_model="openrouter:deepseek/deepseek-v4-flash-0731",
    )

    resp = await client.post(
        f"/api/conversations/{conv['id']}/codex-goal",
        json={"action": "set", "objective": "This should not start Codex"},
    )

    assert resp.status_code == 400
    assert "/goal is only available for Codex" in resp.json()["detail"]


async def test_openrouter_generation_dispatches_to_claude_handler(monkeypatch):
    import server

    conv = await db.create_conversation("OpenRouter Dispatch", mode="openrouter")
    await db.update_conversation_fields(
        conv["id"],
        cc_model="openrouter:deepseek/deepseek-v4-flash-0731",
    )
    calls = []

    async def fake_claude_generation(websocket, conv_id, loaded_conv, data):
        calls.append(("claude", conv_id, loaded_conv["mode"], loaded_conv["cc_model"]))

    async def fake_weave_generation(*args, **kwargs):
        raise AssertionError("OpenRouter generation must not route through Weave")

    monkeypatch.setattr(server, "_handle_claude_generation", fake_claude_generation)
    monkeypatch.setattr(server, "_handle_weave_generation", fake_weave_generation)

    await server._handle_generation(
        None,
        conv["id"],
        {"action": "generate", "cc_model": "openrouter:deepseek/deepseek-v4-flash-0731"},
    )

    assert calls == [
        (
            "claude",
            conv["id"],
            "openrouter",
            "openrouter:deepseek/deepseek-v4-flash-0731",
        )
    ]


async def test_codex_goal_endpoint_get_without_session_uses_loom_state(client, mock_llama, monkeypatch):
    import server

    async def fail_manage_goal(*args, **kwargs):
        raise AssertionError("manage_codex_goal should not be called")

    monkeypatch.setattr(server.codex_client, "manage_codex_goal", fail_manage_goal)
    conv = await db.create_conversation("Codex Goal Get", mode="codex", project_dir=".")
    await db.update_conversation_fields(
        conv["id"],
        cc_model="codex-gpt-5.5",
        codex_goal_objective="Keep tests green",
        codex_goal_status="active",
    )

    resp = await client.post(f"/api/conversations/{conv['id']}/codex-goal", json={"action": "get"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "loom"
    assert data["goal"]["objective"] == "Keep tests green"


async def test_codex_goal_endpoint_sets_goal_and_persists(client, mock_llama, tmp_path, monkeypatch):
    import server

    calls = []

    async def fake_manage_goal(action, cwd, **kwargs):
        calls.append({"action": action, "cwd": cwd, **kwargs})
        return {
            "action": action,
            "thread_id": "thr_goal",
            "goal": {
                "threadId": "thr_goal",
                "objective": kwargs["objective"],
                "status": kwargs["status"],
                "tokenBudget": kwargs["token_budget"],
                "tokensUsed": 5,
                "timeUsedSeconds": 2,
            },
        }

    monkeypatch.setattr(server.codex_client, "manage_codex_goal", fake_manage_goal)
    conv = await db.create_conversation("Codex Goal Set", mode="codex", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], cc_model="codex-gpt-5.5")

    resp = await client.post(
        f"/api/conversations/{conv['id']}/codex-goal",
        json={
            "action": "set",
            "objective": "Finish the migration",
            "token_budget": 40000,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["thread_id"] == "thr_goal"
    assert data["goal"]["objective"] == "Finish the migration"
    assert data["goal"]["tokenBudget"] == 40000
    assert calls[0]["action"] == "set"
    assert calls[0]["resume_session_id"] is None

    updated = await db.get_conversation(conv["id"])
    assert updated["claude_session_id"] == "thr_goal"
    assert updated["codex_goal_objective"] == "Finish the migration"
    assert updated["codex_goal_status"] == "active"
    assert updated["codex_goal_token_budget"] == 40000
    assert updated["codex_goal_tokens_used"] == 5


async def test_create_nrol_operator_conversation(client, mock_llama):
    """POST /api/conversations with nrol_operator launches a locked CC profile.

    Mode is forced to claude, the flag persists, and project_dir defaults to
    the neutral operator workspace (never the Loom or engine repo)."""
    resp = await client.post("/api/conversations", json={
        "title": "NROL Operator Test",
        "mode": "claude",
        "nrol_operator": True,
        "cc_model": "opus",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "claude"
    assert data["nrol_operator"] == 1
    assert data["project_dir"].replace("\\", "/").endswith("workspaces/nrol_operator")


async def test_get_conversation(client, mock_llama):
    """GET /api/conversations/{id} returns the conversation."""
    create_resp = await client.post("/api/conversations", json={"title": "Get Test"})
    conv_id = create_resp.json()["id"]

    resp = await client.get(f"/api/conversations/{conv_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == conv_id
    assert resp.json()["title"] == "Get Test"


async def test_list_conversations(client, mock_llama):
    """GET /api/conversations returns a list."""
    await client.post("/api/conversations", json={"title": "List A"})
    await client.post("/api/conversations", json={"title": "List B"})

    resp = await client.get("/api/conversations")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    titles = [c["title"] for c in data]
    assert "List A" in titles
    assert "List B" in titles


async def test_delete_conversation(client, mock_llama):
    """DELETE /api/conversations/{id} removes the conversation."""
    create_resp = await client.post("/api/conversations", json={"title": "Delete Me"})
    conv_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/conversations/{conv_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    get_resp = await client.get(f"/api/conversations/{conv_id}")
    assert get_resp.status_code == 404


async def test_refresh_cc_models_requires_claude_code_token(client, monkeypatch):
    import server

    monkeypatch.setattr(server, "_read_oauth_token", lambda: None)
    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeAnthropicClient)
    _FakeAnthropicClient.last_request = None

    resp = await client.post("/api/cc-models/refresh")

    assert resp.status_code == 401
    assert "No Claude Code login found" in resp.json()["detail"]
    assert "Run `claude` once" in resp.json()["detail"]
    assert _FakeAnthropicClient.last_request is None


async def test_refresh_cc_models_reports_rejected_claude_code_token(client, monkeypatch):
    import server

    monkeypatch.setattr(server, "_read_oauth_token", lambda: "expired-token")
    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeAnthropicClient)
    _FakeAnthropicClient.response = _FakeAnthropicResponse(401, text="unauthorized")
    _FakeAnthropicClient.last_request = None

    resp = await client.post("/api/cc-models/refresh")

    assert resp.status_code == 401
    assert "Anthropic rejected the login token" in resp.json()["detail"]
    assert "Run `claude`" in resp.json()["detail"]
    assert _FakeAnthropicClient.last_request["headers"]["Authorization"] == "Bearer expired-token"
    assert _FakeAnthropicClient.last_request["headers"]["anthropic-beta"] == "oauth-2025-04-20"


async def test_refresh_cc_models_accepts_valid_claude_code_token(client, monkeypatch):
    import server

    monkeypatch.setattr(server, "_read_oauth_token", lambda: "valid-token")
    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeAnthropicClient)
    monkeypatch.setattr(server, "load_local_codex_models", lambda: [])
    monkeypatch.setattr(server, "load_local_gemini_models", lambda: [])
    monkeypatch.setattr(server, "CC_MODELS", [
        {"group": "Anthropic", "models": []},
        {"group": "Other", "models": [{"value": "other", "label": "Other"}]},
    ])
    _FakeAnthropicClient.response = _FakeAnthropicResponse(200, {
        "data": [
            {"id": "claude-opus-4-7-20260101", "display_name": "Claude Opus 4.7"},
            {"id": "claude-sonnet-4-5-20260101", "display_name": "Claude Sonnet 4.5"},
            {"id": "claude-haiku-4-5-20260101", "display_name": "Claude Haiku 4.5"},
        ]
    })
    _FakeAnthropicClient.last_request = None

    resp = await client.post("/api/cc-models/refresh")

    assert resp.status_code == 200
    data = resp.json()
    assert data["families"] == ["haiku", "opus", "sonnet"]
    groups = {group["group"]: group["models"] for group in data["models"]}
    auto_models = next(models for name, models in groups.items() if "Auto" in name)
    pinned_models = next(models for name, models in groups.items() if "Pinned" in name)
    assert [model["value"] for model in auto_models] == [
        "opus",
        "opus[1m]",
        "sonnet",
        "sonnet[1m]",
        "haiku",
    ]
    pinned_values = [model["value"] for model in pinned_models]
    assert "claude-opus-4-7-20260101[1m]" in pinned_values
    assert "claude-sonnet-4-5-20260101[1m]" in pinned_values
    assert "claude-haiku-4-5-20260101[1m]" not in pinned_values


def test_load_local_gemini_models_and_mapping(tmp_path, monkeypatch):
    import server
    import gemini_client

    # Write a dummy models_cache.json
    gemini_dir = tmp_path / ".gemini"
    gemini_dir.mkdir()
    cache_file = gemini_dir / "models_cache.json"
    cache_file.write_text(
        '{"models": [{"slug": "gemini-3.5-flash-custom", "display_name": "Gemini 3.5 Flash Custom"}]}',
        encoding="utf-8"
    )

    # Monkeypatch home directory path detection
    def mock_home():
        return tmp_path
    monkeypatch.setattr(server.os.environ, "get", lambda k, default=None: str(tmp_path) if k == "USERPROFILE" else default)
    monkeypatch.setattr(server.Path, "home", mock_home)

    models = server.load_local_gemini_models()
    assert len(models) == 1
    assert models[0]["value"] == "gemini:gemini-3.5-flash-custom"
    assert models[0]["label"] == "Gemini (Gemini 3.5 Flash Custom)"

    # Discovered selectors are exact launch identities and must not be rewritten
    # by a stale conversation effort setting.
    mapped = gemini_client._loom_model_to_agy("gemini:gemini-3.5-flash-custom", "high")
    assert mapped == "gemini-3.5-flash-custom"

    mapped_raw = gemini_client._loom_model_to_agy("gemini:some-other-slug", "medium")
    assert mapped_raw == "some-other-slug"  # falls back to slug itself after stripping

    mapped_mixed = gemini_client._loom_model_to_agy("Gemini:some-other-slug", "medium")
    assert mapped_mixed == "some-other-slug"  # case-insensitive check works


def test_antigravity_exact_selector_controls_effort():
    import gemini_client

    selector = "gemini:gemini-3.7-flash-low"
    assert gemini_client._loom_model_to_agy(selector, "high") == "gemini-3.7-flash-low"
    assert gemini_client._agy_effort_for_model(selector, "high") == "low"

    legacy = "Gemini 3.6 Pro (High)"
    assert gemini_client._loom_model_to_agy(legacy, "low") == "gemini-3.6-pro-high"
    assert gemini_client._agy_effort_for_model(legacy, "low") == "high"
