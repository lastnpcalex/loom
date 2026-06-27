"""Shared fixtures for Loom test suite."""

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import httpx

import database as db


def pytest_configure(config):
    # pytest's default basetemp root (%TEMP%/pytest-of-<user>) can be created
    # with an unreadable owner SID on Windows (e.g. by a sandboxed run); once
    # that happens every tmp_path fixture dies in PermissionError after a long
    # retry loop. Redirect to a private root unless the caller passed one.
    # Outside the repo on purpose: a OneDrive-synced basetemp invites sync
    # locks on test SQLite files.
    if config.option.basetemp is None:
        base = Path(tempfile.gettempdir()) / "loom-pytest"
        try:
            base.mkdir(parents=True, exist_ok=True)
            # Try creating a temporary test file in it to verify write permissions
            test_file = base / ".pytest_write_test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink()
        except OSError:
            import getpass
            username = getpass.getuser()
            base = Path(tempfile.gettempdir()) / f"loom-pytest-{username}"
        config.option.basetemp = base


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
    Mocks llama health_check to avoid real network calls.
    """
    with patch("local_summary.preload", new_callable=AsyncMock) as mock_preload, \
         patch("server.local_summary.preload", new_callable=AsyncMock):
        from server import app
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture
def mock_llama():
    """Patch llama_client functions to avoid real network calls."""
    health_result = {
        "status": "ok",
        "models": ["Qwen3.6-27B-NVFP4.gguf"],
        "target_model": "Qwen3.6-27B-NVFP4.gguf",
        "model_available": True,
        "mock_mode": False,
    }

    async def fake_stream(*args, **kwargs):
        for token in ["Hello", " from", " mock", " Llama", "!"]:
            yield token

    with patch("server.health_check", new_callable=AsyncMock, return_value=health_result) as mock_hc, \
         patch("server.stream_chat", side_effect=fake_stream) as mock_sc:
        yield {"health_check": mock_hc, "stream_chat": mock_sc}
