"""Tests for REST API endpoints."""

import pytest


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
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "weave"
    assert data["character_id"] is None
    assert data["persona_id"] is None
    assert data["lore_ids"] == "[]"
    assert data["system_only"] == 1
    assert data["system_prompt"] == "Use only branch history and this instruction."
    assert data["ooda_enabled"] == 0


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

    # Test _loom_model_to_agy mapping strips gemini: prefix and supports mapping
    mapped = gemini_client._loom_model_to_agy("gemini:gemini-3.5-flash-custom", "high")
    assert mapped == "gemini-3.5-flash-medium"  # mapped by gemini-3.5-flash pattern check

    mapped_raw = gemini_client._loom_model_to_agy("gemini:some-other-slug", "medium")
    assert mapped_raw == "some-other-slug"  # falls back to slug itself after stripping

    mapped_mixed = gemini_client._loom_model_to_agy("Gemini:some-other-slug", "medium")
    assert mapped_mixed == "some-other-slug"  # case-insensitive check works


