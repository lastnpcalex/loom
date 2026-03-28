"""Shared fixtures for Loom test suite."""

import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest
import httpx

import database as db


@pytest.fixture(autouse=True)
async def tmp_database(tmp_path):
    """Override DB_PATH to a temp file for every test, run migrations, clean up after."""
    db_file = str(tmp_path / "test_loom.db")
    original = db.DB_PATH
    db.DB_PATH = db_file
    await db.init_db()
    yield db_file
    await db.close_db()
    db.DB_PATH = original


@pytest.fixture
async def client():
    """Async HTTP test client using httpx + ASGITransport.

    Mocks local_summary.preload so we never load Gemma in tests.
    Mocks ollama health_check to avoid real network calls.
    """
    with patch("local_summary.preload", new_callable=AsyncMock) as mock_preload, \
         patch("server.local_summary.preload", new_callable=AsyncMock):
        from server import app
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture
def mock_ollama():
    """Patch ollama_client functions to avoid real network calls."""
    health_result = {
        "status": "ok",
        "models": ["llama3:8b", "qwen3:4b"],
        "target_model": "llama3:8b",
        "model_available": True,
        "mock_mode": False,
    }

    async def fake_stream(*args, **kwargs):
        for token in ["Hello", " from", " mock", " Ollama", "!"]:
            yield token

    with patch("ollama_client.health_check", new_callable=AsyncMock, return_value=health_result) as mock_hc, \
         patch("server.health_check", new_callable=AsyncMock, return_value=health_result), \
         patch("ollama_client.stream_chat", side_effect=fake_stream) as mock_sc:
        yield {"health_check": mock_hc, "stream_chat": mock_sc}
