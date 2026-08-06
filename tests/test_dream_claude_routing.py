import pytest
from pathlib import Path


def test_dream_claude_selector_is_distinct_from_dream_space():
    import server

    value = f"dream:{server.config.dream_model}"

    assert server.is_dream_claude_model(value)
    assert server._dream_claude_model_id(value) == server.config.dream_model
    assert server._nrol_operator_block_reason(value) is not None

    groups = {g["group"]: g["models"] for g in server.CC_MODELS}
    assert "Dream via Claude Code" in groups
    assert any(m["value"] == value for m in groups["Dream via Claude Code"])


def test_dream_claude_prompt_identifies_local_dream_backend():
    import claude_client

    prompt = claude_client._loom_append_system_prompt(
        None,
        use_dream=True,
        cc_model="diffusiongemma-26b-a4b-it-nvfp4",
    )

    assert "local Dream DiffusionGemma sidecar" in prompt
    assert "not a native vision transport" in prompt


@pytest.mark.asyncio
async def test_braid_dream_model_routes_through_dream_shim(monkeypatch):
    import server

    seen = {}

    async def fake_handle(_websocket, conv_id, conv, data):
        seen["conv_id"] = conv_id
        seen["conv"] = conv
        seen["data"] = data

    monkeypatch.setattr(server, "_handle_claude_generation", fake_handle)

    conv = {"local_model": server.config.dream_model}
    await server._handle_local_generation(None, 123, conv, {"action": "generate"})

    assert seen["conv"]["cc_model"] == f"dream:{server.config.dream_model}"
    assert seen["conv"]["_use_llama"] is False


@pytest.mark.asyncio
async def test_dream_stream_floors_tiny_requested_max_tokens(monkeypatch):
    import llama_client

    seen = {}

    class FakeStreamResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aread(self):
            return b""

        async def aiter_lines(self):
            yield (
                'data: {"choices":[{"delta":{"content":"ok"}}],'
                '"usage":{"prompt_tokens":11,"completion_tokens":42}}'
            )
            yield "data: [DONE]"

    class FakeClient:
        def stream(self, method, url, json, headers, timeout):
            seen["method"] = method
            seen["url"] = url
            seen["payload"] = json
            return FakeStreamResponse()

    monkeypatch.setattr(llama_client, "_client", lambda: FakeClient())
    monkeypatch.setattr(llama_client.config, "dream_min_output_tokens", 2048)
    llama_client._mock_mode = False

    chunks = []
    async for chunk in llama_client.stream_chat(
        [{"role": "user", "content": "hello"}],
        max_tokens=128,
        model=llama_client.config.dream_model,
    ):
        chunks.append(chunk)

    assert seen["payload"]["max_tokens"] == 2048
    assert seen["payload"]["chat_template_kwargs"] == {"enable_thinking": True}
    assert chunks[0] == "ok"
    assert chunks[-1]["type"] == "usage"
    assert chunks[-1]["input_tokens"] == 11
    assert chunks[-1]["output_tokens"] == 42
    assert chunks[-1]["content_chunks"] == 1
    assert "canvas_tokens" not in chunks[-1]


@pytest.mark.asyncio
async def test_dream_stream_splits_channel_thinking_from_visible_content(monkeypatch):
    import llama_client

    class FakeStreamResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aread(self):
            return b""

        async def aiter_lines(self):
            yield (
                'data: {"choices":[{"delta":{"content":'
                '"<|channel>thought\\nhidden\\n<channel|>visible"}}]}'
            )
            yield "data: [DONE]"

    class FakeClient:
        def stream(self, method, url, json, headers, timeout):
            return FakeStreamResponse()

    monkeypatch.setattr(llama_client, "_client", lambda: FakeClient())
    llama_client._mock_mode = False

    chunks = []
    async for chunk in llama_client.stream_chat(
        [{"role": "user", "content": "hello"}],
        model=llama_client.config.dream_model,
    ):
        chunks.append(chunk)

    assert chunks[:4] == [
        {"type": "thinking_start"},
        "hidden",
        {"type": "thinking_end"},
        "visible",
    ]


def test_diffusion_gemma_server_reports_length_finish_reason():
    source = Path("vendored/diffusion-gemma-server/diffusion-gemma-server.cpp").read_text(
        encoding="utf-8"
    )

    assert "hit_length" in source
    assert 'return r.hit_length ? "length" : "stop";' in source
    assert 'msg.tool_calls.empty() ? "stop" : "tool_calls"' not in source


def test_diffusion_gemma_server_thinking_is_request_configurable():
    source = Path("vendored/diffusion-gemma-server/diffusion-gemma-server.cpp").read_text(
        encoding="utf-8"
    )

    assert 'body["chat_template_kwargs"]' in source
    assert "DREAM_ENABLE_THINKING" in source
    assert "inputs.enable_thinking = false;" not in source


def test_settings_max_tokens_allows_dream_context_scale():
    html = Path("static/index.html").read_text(encoding="utf-8")

    assert 'id="cfg-max-tokens" step="256" min="64" max="131072"' in html
    assert 'id="cfg-dream-min-output-tokens" min="256" step="256"' in html
