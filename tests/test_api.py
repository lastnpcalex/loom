"""Tests for REST API endpoints."""

import pytest


async def test_health_endpoint(client, mock_ollama):
    """GET /api/health returns status info."""
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "models" in data


async def test_ollama_models_endpoint(client, mock_ollama):
    """GET /api/ollama/models returns model list."""
    resp = await client.get("/api/ollama/models")
    assert resp.status_code == 200
    data = resp.json()
    assert "models" in data
    assert "llama3:8b" in data["models"]


async def test_create_weave_conversation(client, mock_ollama):
    """POST /api/conversations with weave mode."""
    resp = await client.post("/api/conversations", json={
        "title": "Weave Test",
        "mode": "weave",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Weave Test"
    assert data["mode"] == "weave"


async def test_create_local_conversation(client, mock_ollama):
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


async def test_create_claude_conversation(client, mock_ollama):
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


async def test_get_conversation(client, mock_ollama):
    """GET /api/conversations/{id} returns the conversation."""
    create_resp = await client.post("/api/conversations", json={"title": "Get Test"})
    conv_id = create_resp.json()["id"]

    resp = await client.get(f"/api/conversations/{conv_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == conv_id
    assert resp.json()["title"] == "Get Test"


async def test_list_conversations(client, mock_ollama):
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


async def test_delete_conversation(client, mock_ollama):
    """DELETE /api/conversations/{id} removes the conversation."""
    create_resp = await client.post("/api/conversations", json={"title": "Delete Me"})
    conv_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/conversations/{conv_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    get_resp = await client.get(f"/api/conversations/{conv_id}")
    assert get_resp.status_code == 404
