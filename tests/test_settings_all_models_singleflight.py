"""Regression tests for the /api/local/all-models single-flight guard.

The settings panel fires several GET /api/local/all-models requests in the
same tick (4 Weave dropdowns + populateCCModelDropdowns). Before the
single-flight lock, every concurrent caller independently re-probed
llama-server (5s timeout) and the dream sidecar (2s timeout), stacking
latency into the 2s+ settings-open delay. These tests pin the invariant that
concurrent callers share one refresh rather than each re-probing.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_all_engines_cache():
    """Clear the module-level cache so each test starts cold."""
    import server
    server._ALL_ENGINES_CACHE = None
    yield
    server._ALL_ENGINES_CACHE = None


async def test_concurrent_callers_share_one_refresh(client):
    """N concurrent requests trigger the llama + dream probes at most once.

    A counting gate with a small await inside each probe forces overlap: if
    single-flight were absent, the N requests would each enter the refresh
    before the first finished. We assert each probe ran exactly once.
    """
    import server

    llama_calls = 0
    dream_calls = 0
    gate = asyncio.Event()

    async def fake_health_check():
        nonlocal llama_calls
        llama_calls += 1
        await gate.wait()  # hold the refresh in flight so all N requests pile up
        return {"models": [], "model_name_map": {}}

    async def fake_dream_health(host, timeout=3.0):
        nonlocal dream_calls
        dream_calls += 1
        await gate.wait()
        return None

    with patch("llama_client.health_check", new=AsyncMock(side_effect=fake_health_check)), \
         patch("dream_client.health", new=AsyncMock(side_effect=fake_dream_health)):
        # Fire several concurrent requests with a cold cache.
        n = 5
        task = asyncio.gather(*[client.get("/api/local/all-models") for _ in range(n)])
        # Let the event loop schedule all five requests.
        await asyncio.sleep(0.05)
        gate.set()  # release the refresh
        responses = await task

    assert len(responses) == n
    assert all(r.status_code == 200 for r in responses)
    # The invariant: one refresh total, not N.
    assert llama_calls == 1, f"health_check called {llama_calls}x (expected 1)"
    assert dream_calls == 1, f"dream_client.health called {dream_calls}x (expected 1)"


async def test_second_request_within_ttl_skips_refresh(client):
    """Within the TTL window a second request must not re-probe."""
    import server

    llama_calls = 0

    async def fake_health_check():
        nonlocal llama_calls
        llama_calls += 1
        return {"models": ["TestModel.gguf"], "model_name_map": {}}

    with patch("llama_client.health_check", new=AsyncMock(side_effect=fake_health_check)), \
         patch("dream_client.health", new=AsyncMock(return_value=None)):
        first = await client.get("/api/local/all-models")
        assert first.status_code == 200
        assert llama_calls == 1

        # Second call within the TTL window should be served from cache.
        second = await client.get("/api/local/all-models")
        assert second.status_code == 200
        assert llama_calls == 1, "second call within TTL re-probed (cache not served)"

    # Sanity: the cached payload is identical across calls.
    assert first.json()["models"] == second.json()["models"]
