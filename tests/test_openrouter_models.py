"""OpenRouter model-registration invariants.

Two bug classes are pinned here:

1. Cross-file drift — a model slug added to one registry (openrouter_client,
   server.CC_MODELS, model_context, static/*.js) but missed in another. Cheap,
   offline, always runs.
2. Invented slugs — a slug that looks plausible but does not exist upstream, so
   the model only fails at generation time with "It may not exist or you may not
   have access to it". Needs the network + a key, so it is opt-in.
"""

import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _registered_slugs() -> set[str]:
    import openrouter_client

    return {
        openrouter_client.model_slug(m["name"]) for m in openrouter_client.DEFAULT_MODELS
    }


def test_registered_slugs_are_recognized_everywhere():
    """Every curated slug must be classified as OpenRouter by both classifiers."""
    import model_context
    import openrouter_client

    slugs = _registered_slugs()
    assert slugs, "no OpenRouter models registered"

    for slug in slugs:
        prefixed = f"{openrouter_client.MODEL_PREFIX}{slug}"
        assert openrouter_client.is_openrouter_model(prefixed), slug
        assert openrouter_client.is_openrouter_model(slug), slug
        assert model_context.is_openrouter(prefixed), slug
        assert model_context.is_openrouter(slug), slug
        # An OpenRouter model must never fall into the local-llama bucket, or it
        # would inherit the wrong handoff threshold.
        assert not model_context.is_local_llama(prefixed), slug


def test_registered_slugs_appear_in_cc_model_picker():
    import server

    groups = {g["group"]: g["models"] for g in server.CC_MODELS}
    assert "OpenRouter" in groups, "OpenRouter group missing from CC_MODELS"
    picker = {m["value"] for m in groups["OpenRouter"]}

    for slug in _registered_slugs():
        assert f"openrouter:{slug}" in picker, f"{slug} missing from CC_MODELS picker"


def test_registered_slugs_have_explicit_handoff_thresholds():
    """A slug with no threshold silently inherits the 200k Anthropic default."""
    import model_context

    for slug in _registered_slugs():
        threshold = model_context.handoff_threshold(f"openrouter:{slug}")
        assert threshold != model_context.THRESHOLD_ANTHROPIC_STD, (
            f"{slug} has no explicit threshold in model_context.handoff_threshold"
        )


@pytest.mark.parametrize("script", ["app.js", "chat.js"])
def test_frontend_knows_every_registered_slug(script):
    """The bare slug aliases in the JS classifiers must match the Python registry."""
    source = (REPO / "static" / script).read_text(encoding="utf-8", errors="replace")
    for slug in _registered_slugs():
        assert slug in source, f"{slug} missing from static/{script}"


def test_openrouter_stream_sends_reasoning_on_the_thinking_channel(monkeypatch):
    """Reasoning deltas must not be yielded as bare strings.

    Weave/OODA concatenate every bare string into the visible message body, so a
    bare reasoning delta leaks chain-of-thought into chat for reasoning models
    (deepseek-v4-flash, glm-5.2, kimi-k2.7).
    """
    import asyncio

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
            yield 'data: {"choices":[{"delta":{"reasoning":"secret plan"}}]}'
            yield 'data: {"choices":[{"delta":{"content":"visible answer"}}]}'
            yield "data: [DONE]"

    class FakeClient:
        def stream(self, method, url, json, headers, timeout):
            return FakeStreamResponse()

    monkeypatch.setattr(llama_client, "_client", lambda: FakeClient())
    monkeypatch.setattr(
        llama_client.openrouter_client, "ensure_budget_available", _noop_async
    )

    async def collect():
        out = []
        async for item in llama_client.stream_chat(
            [{"role": "user", "content": "hi"}],
            model="openrouter:deepseek/deepseek-v4-flash-0731",
        ):
            out.append(item)
        return out

    items = asyncio.run(collect())

    visible = "".join(i for i in items if isinstance(i, str))
    thinking = "".join(
        i["text"] for i in items if isinstance(i, dict) and i.get("type") == "thinking_delta"
    )

    assert visible == "visible answer"
    assert thinking == "secret plan"
    assert "secret plan" not in visible


async def test_openrouter_budget_preflight_uses_short_lived_key_cache(monkeypatch):
    import openrouter_client

    calls = 0

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": {"usage_weekly": 1.0, "usage_monthly": 2.0}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers):
            nonlocal calls
            calls += 1
            return FakeResponse()

    openrouter_client._key_status_cache.update({
        "token": "",
        "fetched_at": 0.0,
        "status": None,
    })
    monkeypatch.setattr(openrouter_client, "api_key", lambda: "sk-or-test")
    monkeypatch.setattr(openrouter_client.httpx, "AsyncClient", FakeClient)

    await openrouter_client.ensure_budget_available(projected_cost_usd=0.01)
    await openrouter_client.ensure_budget_available(projected_cost_usd=0.01)

    assert calls == 1


async def _noop_async(*args, **kwargs):
    return None


@pytest.mark.skipif(
    not os.environ.get("LOOM_TEST_OPENROUTER_LIVE"),
    reason="set LOOM_TEST_OPENROUTER_LIVE=1 to validate slugs against the live catalog",
)
def test_registered_slugs_exist_upstream():
    """Catches invented/misremembered slugs before they fail at generation time."""
    import httpx

    import openrouter_client

    catalog = httpx.get("https://openrouter.ai/api/v1/models", timeout=60).json()
    available = {m["id"] for m in catalog.get("data", [])}
    missing = sorted(_registered_slugs() - available)
    assert not missing, f"slugs not present on OpenRouter: {missing}"
