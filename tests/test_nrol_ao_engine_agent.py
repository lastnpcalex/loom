"""Focused tests for the NROL-AO engine-agent package (Track A phase 1).

Three layers:
  1. Tool schema + dispatch (no Dream): fetch_article returns the structured
     shape, errors are surfaced not raised, unknown tools raise KeyError.
  2. dream_client parsing + channel strip (no network): malformed-JSON
     handling, channel-markup strip on stop-turn text.
  3. engine_agent loop with a FAKE Dream client: multi-turn dispatch,
     stop-terminates, max-turns cap, and fail-closed on repeated malformed
     arguments. No live Dream required for any of these.

A live-marked end-to-end test (real Dream :8787 round-trip through the agent)
lives at the bottom and is skipped when the sidecar is down — the normal suite
never depends on the GPU.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import httpx
import pytest

from mcp_servers.nrol_ao_engine import dream_client, engine_agent
from mcp_servers.nrol_ao_engine.tools import TOOLS, fetch


# ──────────────────────────────────────────────────────────────────────────
# 1. Tool schema + dispatch (no Dream)
# ──────────────────────────────────────────────────────────────────────────


def test_fetch_article_schema_is_valid_openai_tool_spec():
    specs = engine_agent._tool_specs()
    # Phase 2 added reading + advocate tools; fetch_article must still be present.
    names = {s["function"]["name"] for s in specs}
    assert "fetch_article" in names
    spec = next(s for s in specs if s["function"]["name"] == "fetch_article")
    assert spec["type"] == "function"
    fn = spec["function"]
    assert fn["name"] == "fetch_article"
    params = fn["parameters"]
    assert params["type"] == "object"
    assert "url" in params["properties"]
    assert params["required"] == ["url"]
    # JSON-serializable (Dream rejects non-serializable tool specs).
    json.dumps(spec)


def test_fetch_article_empty_url_returns_error_dict_not_raises():
    result = fetch.fetch_article("")
    assert isinstance(result, dict)
    assert result["error"] is not None
    assert result["text"] == ""
    # Structured fields always present.
    for key in ("url", "headline", "source", "published_at", "text", "error"):
        assert key in result


def test_fetch_article_dispatch_through_registry():
    """The agent loop dispatches via TOOLS[name]['fn']; verify that path."""
    fn = TOOLS["fetch_article"]["fn"]
    result = fn("")
    assert result["error"] is not None


def test_dispatch_unknown_tool_raises_keyerror():
    with pytest.raises(KeyError):
        engine_agent._dispatch_tool("does_not_exist", {})


def test_fetch_article_source_extracted_from_url(monkeypatch):
    """Source falls back to URL netloc when trafilatura metadata is absent."""
    # Force trafilatura to be considered missing so we hit the early error
    # path but still get a source from the URL.
    monkeypatch.setitem(__import__("sys").modules, "trafilatura", None)
    result = fetch.fetch_article("https://www.example.com/news/story")
    assert result["source"] == "example.com"
    assert result["error"] == "trafilatura not installed"


# ──────────────────────────────────────────────────────────────────────────
# 2. dream_client parsing + channel strip (no network)
# ──────────────────────────────────────────────────────────────────────────


def test_parse_tool_args_valid_json_string():
    parsed, err = dream_client.parse_tool_args('{"url": "https://x", "max_chars": 100}')
    assert err is None
    assert parsed == {"url": "https://x", "max_chars": 100}


def test_parse_tool_args_already_dict_passes_through():
    parsed, err = dream_client.parse_tool_args({"url": "x"})
    assert err is None
    assert parsed == {"url": "x"}


def test_parse_tool_args_malformed_json_returns_error():
    parsed, err = dream_client.parse_tool_args('{"url": broken')
    assert parsed is None
    assert "JSONDecodeError" in err


def test_parse_tool_args_non_object_json_returns_error():
    parsed, err = dream_client.parse_tool_args('["not", "an", "object"]')
    assert parsed is None
    assert "not object" in err


def test_parse_tool_args_wrong_type_returns_error():
    parsed, err = dream_client.parse_tool_args(42)
    assert parsed is None
    assert "not str/dict" in err


def test_strip_channel_scaffold_removes_thought_markup():
    text = "<|channel>thought\nhidden reasoning\n<channel|>\nVisible answer"
    stripped = dream_client._strip_channel_scaffold(text)
    assert "<|channel>" not in stripped
    assert "<channel|>" not in stripped
    assert "Visible answer" in stripped
    assert "hidden reasoning" not in stripped


def test_strip_channel_scaffold_passthrough_when_no_markup():
    assert dream_client._strip_channel_scaffold("clean text") == "clean text"


def test_resolve_host_rewrites_localhost_to_ipv4():
    assert dream_client.resolve_host("http://localhost:8787") == "http://127.0.0.1:8787"
    assert dream_client.resolve_host("127.0.0.1:8787") == "http://127.0.0.1:8787"


class _FakeHTTPResponse:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data or {}
        self.text = text

    def json(self):
        return self._data


class _FakeHTTPClient:
    def __init__(self, response, calls):
        self._response = response
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json):
        self._calls.append({"url": url, "json": json})
        return self._response


def test_chat_with_tools_enables_thinking_by_default(monkeypatch):
    calls = []
    response = _FakeHTTPResponse(data={
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": "done"},
        }],
        "usage": {},
    })

    monkeypatch.setattr(
        dream_client.httpx,
        "Client",
        lambda timeout: _FakeHTTPClient(response, calls),
    )

    result = dream_client.chat_with_tools(
        [{"role": "user", "content": "hi"}],
        tools=[],
        host="http://dream.test",
    )

    assert result["content"] == "done"
    payload = calls[0]["json"]
    assert payload["chat_template_kwargs"] == {"enable_thinking": True}


def test_chat_with_tools_http_error_includes_request_summary(monkeypatch):
    calls = []
    response = _FakeHTTPResponse(
        status_code=400,
        text="failed to format chat request: tool message mismatch",
    )

    monkeypatch.setattr(
        dream_client.httpx,
        "Client",
        lambda timeout: _FakeHTTPClient(response, calls),
    )

    messages = [
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("c1", "fetch_article", {"url": "x"})]},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ]
    with pytest.raises(RuntimeError) as excinfo:
        dream_client.chat_with_tools(
            messages,
            tools=[fetch.SCHEMA],
            host="http://dream.test",
            max_tokens=123,
            temperature=0.1,
        )

    msg = str(excinfo.value)
    assert "failed to format chat request" in msg
    assert "request_summary" in msg
    assert "'messages': 3" in msg
    assert "'tool_call_id': 'c1'" in msg
    assert "'enable_thinking': True" in msg


# ──────────────────────────────────────────────────────────────────────────
# 3. engine_agent loop with a FAKE Dream client
# ──────────────────────────────────────────────────────────────────────────


class _FakeDream:
    """Records calls and replays a scripted list of responses.

    Each response is a dict shaped like dream_client.chat_with_tools output:
        {"finish_reason": str, "content": str, "tool_calls": [...], "usage": {}}
    """

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), "kwargs": kwargs})
        if not self._responses:
            raise AssertionError("FakeDream exhausted: agent loop called more times than scripted")
        return self._responses.pop(0)


def _tool_call(call_id: str, name: str, arguments: dict | str):
    args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def test_agent_loop_dispatches_tool_then_stops(monkeypatch):
    """Turn 1: model calls fetch_article. Turn 2: model stops with text."""
    fake = _FakeDream([
        {
            "finish_reason": "tool_calls",
            "content": "",
            "tool_calls": [_tool_call("c1", "fetch_article", {"url": "https://example.com/a"})],
            "usage": {},
        },
        {
            "finish_reason": "stop",
            "content": "I fetched the article. It describes tanker traffic.",
            "tool_calls": [],
            "usage": {},
        },
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake)

    # Stub fetch_article so no network happens.
    def fake_fetch(url, **kw):
        return {"url": url, "headline": "Tankers", "source": "example.com",
                "published_at": "", "text": "Tanker traffic fell 12%.", "error": None}
    monkeypatch.setitem(TOOLS, "fetch_article", {"schema": fetch.SCHEMA, "fn": fake_fetch})

    trace = engine_agent.run_engine_agent("Read the article at https://example.com/a")

    assert trace["ok"] is True
    assert trace["turns"] == 2
    assert trace["finish_reason"] == "stop"
    assert "tanker traffic" in trace["final_text"].lower()
    assert len(trace["tool_calls"]) == 1
    assert trace["tool_calls"][0]["name"] == "fetch_article"
    # The tool result was appended as a tool-role message before turn 2 —
    # verifies the agent fed the fetch output back to the model.
    turn2_messages = fake.calls[1]["messages"]
    assert any(m.get("role") == "tool" and "Tanker traffic fell" in m.get("content", "")
               for m in turn2_messages)


def test_agent_loop_retries_malformed_args_once(monkeypatch):
    """First malformed JSON -> retry. Second malformed JSON on same call -> fail closed."""
    bad_args = "{not valid json"
    fake = _FakeDream([
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c1", "fetch_article", bad_args)], "usage": {}},
        # Turn 2: the model re-emits the SAME call id with valid args — but we
        # script it to fail AGAIN so the fail-closed path triggers.
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c1", "fetch_article", bad_args)], "usage": {}},
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake)

    trace = engine_agent.run_engine_agent("fetch https://example.com/a")

    assert trace["ok"] is False
    assert trace["error"] is not None
    assert "malformed arguments twice" in trace["error"]


def test_agent_loop_retries_then_succeeds(monkeypatch):
    """Malformed args on turn 1, valid call on turn 2, stop on turn 3."""
    fake = _FakeDream([
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c1", "fetch_article", "{bad json")], "usage": {}},
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c2", "fetch_article", {"url": "https://example.com/b"})],
         "usage": {}},
        {"finish_reason": "stop", "content": "Done after retry.", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake)
    monkeypatch.setitem(TOOLS, "fetch_article", {
        "schema": fetch.SCHEMA,
        "fn": lambda url, **kw: {"url": url, "headline": "h", "source": "s",
                                "published_at": "", "text": "body", "error": None},
    })

    trace = engine_agent.run_engine_agent("fetch and summarize")
    assert trace["ok"] is True
    assert trace["turns"] == 3
    # Turn 1's malformed call was retried; turn 2 succeeded with a NEW call id.
    assert len(trace["tool_calls"]) == 2


def test_agent_loop_hits_max_turns(monkeypatch):
    """A model that never stops calling tools is aborted at max_turns."""
    looping = {
        "finish_reason": "tool_calls",
        "content": "",
        "tool_calls": [_tool_call("c", "fetch_article", {"url": "https://example.com/x"})],
        "usage": {},
    }
    fake = _FakeDream([looping] * 100)
    monkeypatch.setattr(dream_client, "chat_with_tools", fake)
    monkeypatch.setitem(TOOLS, "fetch_article", {
        "schema": fetch.SCHEMA,
        "fn": lambda url, **kw: {"url": url, "headline": "", "source": "",
                                "published_at": "", "text": "body", "error": None},
    })

    trace = engine_agent.run_engine_agent("loop forever", max_turns=4)
    assert trace["ok"] is False
    assert trace["turns"] == 4
    assert "max_turns" in trace["error"]


def test_agent_loop_empty_response_no_tool_calls_ends_with_error(monkeypatch):
    """A non-stop finish with no tool calls (e.g. length) ends the run."""
    fake = _FakeDream([
        {"finish_reason": "length", "content": "partial...", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake)
    trace = engine_agent.run_engine_agent("say hi")
    assert trace["ok"] is False
    assert "length" in trace["error"]


def test_agent_loop_unknown_tool_surfaces_error_to_model(monkeypatch):
    """An unknown tool name is reported back, not a hard abort."""
    fake = _FakeDream([
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c1", "nonexistent_tool", {})], "usage": {}},
        {"finish_reason": "stop", "content": "okay I cannot do that.", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake)
    trace = engine_agent.run_engine_agent("do something")
    assert trace["ok"] is True
    # The unknown-tool error was appended as a tool message the model saw.
    tool_msgs = [m for m in fake.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "unknown tool" in tool_msgs[0]["content"]


def test_force_first_tool_call_sends_required_then_auto(monkeypatch):
    """force_first_tool_call=True: turn 1 uses tool_choice=required, turn 2 auto."""
    fake = _FakeDream([
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c1", "fetch_article", {"url": "https://example.com/a"})],
         "usage": {}},
        {"finish_reason": "stop", "content": "done.", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake)
    monkeypatch.setitem(TOOLS, "fetch_article", {
        "schema": fetch.SCHEMA,
        "fn": lambda url, **kw: {"url": url, "headline": "", "source": "",
                                "published_at": "", "text": "body", "error": None},
    })
    trace = engine_agent.run_engine_agent("fetch and summarize", force_first_tool_call=True)
    assert trace["ok"] is True
    assert fake.calls[0]["kwargs"]["tool_choice"] == "required"
    assert fake.calls[1]["kwargs"]["tool_choice"] == "auto"


def test_default_does_not_force_tool_choice(monkeypatch):
    """Without force_first_tool_call, every turn uses tool_choice=auto."""
    fake = _FakeDream([
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c1", "fetch_article", {"url": "https://example.com/a"})],
         "usage": {}},
        {"finish_reason": "stop", "content": "done.", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake)
    monkeypatch.setitem(TOOLS, "fetch_article", {
        "schema": fetch.SCHEMA,
        "fn": lambda url, **kw: {"url": url, "headline": "", "source": "",
                                "published_at": "", "text": "body", "error": None},
    })
    trace = engine_agent.run_engine_agent("fetch and summarize")
    assert trace["ok"] is True
    assert fake.calls[0]["kwargs"]["tool_choice"] == "auto"


def test_tool_allow_list_sends_only_named_tool_specs(monkeypatch):
    """Stage-scoped runs send only the allowed tools to Dream."""
    fake = _FakeDream([
        {"finish_reason": "stop", "content": "done.", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake)

    trace = engine_agent.run_engine_agent(
        "fetch and summarize",
        tool_names=("fetch_article",),
    )

    assert trace["ok"] is True
    assert trace["allowed_tools"] == ["fetch_article"]
    sent_tools = fake.calls[0]["kwargs"]["tools"]
    assert [spec["function"]["name"] for spec in sent_tools] == ["fetch_article"]


def test_tool_allow_list_blocks_globally_registered_but_disallowed_tool(monkeypatch):
    """A stage cannot dispatch tools outside its allow-list."""
    fake = _FakeDream([
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c1", "read_topic", {"slug": "test-topic"})],
         "usage": {}},
        {"finish_reason": "stop", "content": "saw tool error.", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake)

    trace = engine_agent.run_engine_agent(
        "try an unavailable tool",
        tool_names=("fetch_article",),
        force_first_tool_call=True,
    )

    assert trace["ok"] is True
    assert trace["allowed_tools"] == ["fetch_article"]
    tool_msgs = [m for m in fake.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs
    assert "unknown tool" in tool_msgs[0]["content"]


def test_tool_allow_list_rejects_unknown_tool_name():
    with pytest.raises(KeyError):
        engine_agent._tool_specs(("not_registered",))


# ──────────────────────────────────────────────────────────────────────────
# Live end-to-end (skipped when Dream is down)
# ──────────────────────────────────────────────────────────────────────────


def _dream_up() -> bool:
    host = dream_client.resolve_host()
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{host}/v1/models")
            return r.status_code == 200 and bool(r.json().get("data"))
    except Exception:
        return False


_live = os.environ.get("LOOM_RUN_LIVE_DREAM_PROBE") == "1"
pytestmark_live = pytest.mark.skipif(
    not (_live or _dream_up()),
    reason="Dream sidecar not reachable (set LOOM_RUN_LIVE_DREAM_PROBE=1 to force)",
)


@pytest.mark.live_dream
@pytestmark_live
def test_engine_agent_live_round_trip_fetch_article():
    """End-to-end: the agent fetches a real URL through Dream and stops.

    Uses force_first_tool_call=True because DiffusionGemma is a diffusion
    model, not an instruction-tuned chat model — left to ``auto`` it narrates
    intent rather than calling. Forcing the first turn validates the real
    stack: dream_client → Dream :8787 → tool-call dispatch → fetch_article →
    trafilatura → tool result → stop. A stable, public, text-heavy URL is
    used so the fetch has real content to return.
    """
    url = "https://www.example.com/"
    trace = engine_agent.run_engine_agent(
        f"Use fetch_article to read {url} and then describe in one sentence "
        "what the page says.",
        max_turns=5,
        timeout=300.0,
        force_first_tool_call=True,
    )
    # Turn 1 must have produced a tool call (forced), and it must be fetch_article.
    assert trace["tool_calls"], "agent made no tool calls"
    assert any(c["name"] == "fetch_article" for c in trace["tool_calls"])
    # The fetch_article call's arguments must have parsed to a valid URL.
    fetch_call = next(c for c in trace["tool_calls"] if c["name"] == "fetch_article")
    args, _ = dream_client.parse_tool_args(fetch_call["arguments"])
    assert args and args.get("url"), "fetch_article call had no url argument"
    # The run should have completed within the turn cap.
    assert trace["turns"] >= 1
    assert trace["turns"] <= 5
