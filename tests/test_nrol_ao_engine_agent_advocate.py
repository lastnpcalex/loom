"""Tests for the NROL-AO engine-agent advocate stage (Track A phase 2).

Four layers (mirrors the phase-1 test file structure):
  1. propose_advocate tool: records a proposal, returns a proposal_id, and
     the schema enforces the verdict/kind enums (validation surfaces as a
     structured error, never raises into the loop).
  2. Reading tools: read_recent_evidence caps snippets + handles an empty
     evidenceLog; read_indicator_schema returns a flat list via a mocked
     engine import.
  3. run_advocate with a FAKE Dream client: the runner dispatches
     read_indicator_schema (forced first call) then propose_advocate per
     article, and the analysis field is captured in the harvested proposals.
  4. Live end-to-end against the real Hormuz topic + real Dream: the §4.1
     phase-3 gate — every proposal's analysis > 400 chars AND cites at least
     one indicator id (t[0-9]_ or H[0-9]) or evidence id (ev_). Skipped when
     Dream is down; force with LOOM_RUN_LIVE_DREAM_PROBE=1.
"""

from __future__ import annotations

import json
import os
import re
from unittest.mock import MagicMock

import httpx
import pytest

from mcp_servers.nrol_ao_engine import advocate_agent, dream_client, engine_agent
from mcp_servers.nrol_ao_engine.tools import advocate
from mcp_servers.nrol_ao_engine.tools import read as read_tool
from mcp_servers.nrol_ao_engine.tools import TOOLS


# ──────────────────────────────────────────────────────────────────────────
# 1. propose_advocate tool (no Dream)
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_advocate_store():
    """Each test starts with an empty proposal store."""
    advocate.reset_proposals()
    yield
    advocate.reset_proposals()


def test_propose_advocate_schema_is_valid_openai_tool_spec():
    spec = advocate.SCHEMA
    assert spec["type"] == "function"
    fn = spec["function"]
    assert fn["name"] == "propose_advocate"
    params = fn["parameters"]
    # All five fields required.
    assert set(params["required"]) == {
        "article_id", "verdict", "proposed_action", "citation", "analysis"
    }
    # Enums enforced by the schema.
    assert set(params["properties"]["verdict"]["enum"]) == {
        "COMMIT", "PARK", "WITHDRAW", "DUPLICATE_OF", "SCHEMA_GAP"
    }
    action_kinds = set(params["properties"]["proposed_action"]["properties"]["kind"]["enum"])
    assert action_kinds == {"FIRE", "OBSERVE", "PARK", "IGNORE", "SCHEMA_GAP"}
    # The analysis description demands >400 chars (§6 requirement in the tool
    # description, not the system prompt).
    analysis_desc = params["properties"]["analysis"]["description"]
    assert "400" in analysis_desc
    assert "multi-paragraph" in analysis_desc.lower()
    json.dumps(spec)  # JSON-serializable


def test_propose_advocate_records_proposal_and_returns_id():
    analysis = "A" * 500  # meets the >400 floor
    result = advocate.propose_advocate(
        article_id="A1",
        verdict="PARK",
        proposed_action={"kind": "PARK"},
        citation="the article mentions tankers",
        analysis=analysis,
    )
    assert result["recorded"] is True
    assert result["proposal_id"].startswith("adv_")
    # The proposal is in the in-memory store.
    proposals = advocate.list_proposals()
    assert len(proposals) == 1
    p = proposals[0]
    assert p["proposal_id"] == result["proposal_id"]
    assert p["article_id"] == "A1"
    assert p["verdict"] == "PARK"
    assert p["analysis"] == analysis
    assert p["analysis_len"] == 500
    assert p["analysis_meets_min_len"] is True


def test_propose_advocate_records_short_analysis_with_flag():
    """A sub-400-char analysis is still recorded (audit trail preserved) but flagged."""
    result = advocate.propose_advocate(
        article_id="A2",
        verdict="WITHDRAW",
        proposed_action={"kind": "IGNORE"},
        citation="",
        analysis="too short",  # 9 chars
    )
    assert result["recorded"] is True
    p = advocate.list_proposals()[-1]
    assert p["analysis_len"] < 400
    assert p["analysis_meets_min_len"] is False


def test_propose_advocate_rejects_bad_verdict_with_structured_error():
    result = advocate.propose_advocate(
        article_id="A3",
        verdict="NOT_A_VERDICT",  # not in the enum
        proposed_action={"kind": "PARK"},
        citation="",
        analysis="A" * 500,
    )
    assert "error" in result
    assert "NOT_A_VERDICT" in result["error"]
    # Nothing was recorded.
    assert advocate.list_proposals() == []


def test_propose_advocate_rejects_bad_action_kind_with_structured_error():
    result = advocate.propose_advocate(
        article_id="A4",
        verdict="COMMIT",
        proposed_action={"kind": "EXPLODE"},  # not in the enum
        citation="x",
        analysis="A" * 500,
    )
    assert "error" in result
    assert "EXPLODE" in result["error"]
    assert advocate.list_proposals() == []


def test_propose_advocate_requires_parent_idx_for_duplicate_of():
    result = advocate.propose_advocate(
        article_id="A5",
        verdict="DUPLICATE_OF",
        proposed_action={"kind": "IGNORE"},  # missing parent_idx
        citation="",
        analysis="A" * 500,
    )
    assert "error" in result
    assert "parent_idx" in result["error"]


def test_propose_advocate_requires_indicator_id_for_fire():
    result = advocate.propose_advocate(
        article_id="A6",
        verdict="COMMIT",
        proposed_action={"kind": "FIRE"},  # missing indicator_id
        citation="x",
        analysis="A" * 500,
    )
    assert "error" in result
    assert "indicator_id" in result["error"]


def test_propose_advocate_requires_value_for_observe():
    result = advocate.propose_advocate(
        article_id="A7",
        verdict="COMMIT",
        proposed_action={"kind": "OBSERVE", "indicator_id": "t1_x"},
        citation="x",
        analysis="A" * 500,
    )
    assert "error" in result
    assert "value" in result["error"]


def test_propose_advocate_observe_with_value_and_indicator_succeeds():
    result = advocate.propose_advocate(
        article_id="A8",
        verdict="COMMIT",
        proposed_action={"kind": "OBSERVE", "indicator_id": "t1_transit_band_40_to_55", "value": 42.5},
        citation="transit at 42.5%",
        analysis="A" * 500,
    )
    assert result["recorded"] is True
    p = advocate.list_proposals()[-1]
    assert p["proposed_action"]["value"] == 42.5


# ──────────────────────────────────────────────────────────────────────────
# 2. Reading tools (no Dream, mocked engine import)
# ──────────────────────────────────────────────────────────────────────────


def _fake_topic(*, evidence_log=None, indicators=None, hypotheses=None):
    """Build a minimal topic dict shaped like the real on-disk state."""
    return {
        "meta": {
            "slug": "test-topic",
            "title": "Test Topic",
            "question": "Will X happen?",
            "resolution": "resolves when Y",
            "status": "ACTIVE",
            "classification": "CALIBRATION",
        },
        "model": {
            "hypotheses": hypotheses or {
                "H1": {"label": "yes", "posterior": 0.6},
                "H2": {"label": "no", "posterior": 0.4},
            },
        },
        "evidenceLog": evidence_log or [],
        "indicators": indicators or {
            "tiers": {
                "tier1_critical": [
                    {
                        "id": "t1_indicator_one",
                        "desc": "First test indicator",
                        "likelihoods": {"H1": 0.8, "H2": 0.2},
                        "posteriorEffect": "H1 +10pp; H2 -10pp",
                        "observable": {"metric": "x", "threshold_value": 5, "direction": "higher_strengthens"},
                        "shape": "per_event_member",
                    }
                ],
                "tier2_strong": [],
                "tier3_suggestive": [],
            },
            "anti_indicators": [],
        },
    }


def test_read_topic_strips_evidence_and_indicators(monkeypatch):
    topic = _fake_topic(evidence_log=[{"id": "ev_1", "text": "x" * 1000}],
                       indicators={"tiers": {"tier1_critical": [{"id": "t1_x", "likelihoods": {}}]}})
    monkeypatch.setattr(read_tool, "_load_topic", lambda slug: topic)

    result = read_tool.read_topic("test-topic")
    assert result["slug"] == "test-topic"
    assert result["title"] == "Test Topic"
    assert result["question"] == "Will X happen?"
    assert result["status"] == "ACTIVE"
    assert set(result["hypotheses"].keys()) == {"H1", "H2"}
    assert result["hypotheses"]["H1"] == {"label": "yes", "posterior": 0.6}
    # The big fields are deliberately excluded.
    assert "evidenceLog" not in result
    assert "indicators" not in result


def test_read_recent_evidence_caps_snippets(monkeypatch):
    long_text = "Z" * 1000
    log = [
        {"id": "ev_1", "time": "2026-07-01", "url": "https://a", "text": long_text, "provenance": "OBSERVED"},
        {"id": "ev_2", "time": "2026-07-02", "url": "https://b", "text": "short", "provenance": "PARKED"},
    ]
    monkeypatch.setattr(read_tool, "_load_topic", lambda slug: _fake_topic(evidence_log=log))

    result = read_tool.read_recent_evidence("test-topic", limit=2)
    assert result["count"] == 2
    ev = result["evidence"]
    # Window is last N; ev_2 is most recent.
    assert ev[1]["evidence_id"] == "ev_2"
    # The long snippet is capped to ~300 chars (+ ellipsis).
    assert len(ev[0]["text_snippet"]) <= 305
    assert ev[0]["text_snippet"].endswith("…")
    # The short one is untouched.
    assert ev[1]["text_snippet"] == "short"


def test_read_recent_evidence_handles_empty_log(monkeypatch):
    monkeypatch.setattr(read_tool, "_load_topic", lambda slug: _fake_topic(evidence_log=[]))
    result = read_tool.read_recent_evidence("test-topic")
    assert result["count"] == 0
    assert result["evidence"] == []


def test_read_recent_evidence_load_error_surfaces_as_error(monkeypatch):
    monkeypatch.setattr(read_tool, "_load_topic", lambda slug: {"_load_error": "FileNotFoundError: nope"})
    result = read_tool.read_recent_evidence("missing-topic")
    assert "error" in result
    assert "nope" in result["error"]


def test_read_indicator_schema_returns_flat_list(monkeypatch):
    topic = _fake_topic()
    # Mock import_from_repo to return fakes for both the nop + schema modules.
    fake_nop = MagicMock()
    fake_nop.walk_indicators = lambda t: [
        ind for _tier, ind in __import__(
            "framework.indicator_schema", fromlist=["iter_indicators_for_topic"]
        ).iter_indicators_for_topic(t)
    ] if False else []  # not used — we go through iter_indicators_for_topic below

    # We need a real iter_indicators_for_topic to walk the fake topic. Import
    # the real one from the engine repo is overkill; instead monkeypatch the
    # schema walk to read our fake topic's tiers directly.
    fake_schema_mod = MagicMock()

    def fake_iter(topic):
        tiers = (topic.get("indicators") or {}).get("tiers") or {}
        for tier_key in ("tier1_critical", "tier2_strong", "tier3_suggestive"):
            for ind in tiers.get(tier_key, []):
                yield tier_key, ind
        for ind in (topic.get("indicators") or {}).get("anti_indicators", []):
            yield "anti_indicators", ind

    fake_schema_mod.iter_indicators_for_topic = fake_iter
    fake_nop.walk_indicators = lambda t: [ind for _t, ind in fake_iter(t)]

    def fake_import(module_name):
        if module_name == "engine":
            m = MagicMock()
            m.load_topic = lambda slug: topic
            return m
        if module_name == "framework.news_observation_pipeline":
            return fake_nop
        if module_name == "framework.indicator_schema":
            return fake_schema_mod
        raise ImportError(module_name)

    monkeypatch.setattr(read_tool, "import_from_repo", fake_import)

    result = read_tool.read_indicator_schema("test-topic")
    assert result["slug"] == "test-topic"
    assert result["count"] == 1
    inds = result["indicators"]
    assert inds[0]["id"] == "t1_indicator_one"
    assert inds[0]["tier"] == "tier1_critical"
    assert inds[0]["desc"] == "First test indicator"
    assert inds[0]["likelihoods"] == {"H1": 0.8, "H2": 0.2}
    assert inds[0]["posteriorEffect"] == "H1 +10pp; H2 -10pp"
    assert inds[0]["observable"]["metric"] == "x"
    assert inds[0]["shape"] == "per_event_member"


def test_read_indicator_schema_empty_topic(monkeypatch):
    monkeypatch.setattr(read_tool, "_load_topic", lambda slug: _fake_topic(indicators={"tiers": {}}))
    # Stub the imports so we don't hit the real engine repo.
    fake_schema_mod = MagicMock()
    fake_schema_mod.iter_indicators_for_topic = lambda t: iter([])
    fake_nop = MagicMock()
    fake_nop.walk_indicators = lambda t: []

    def fake_import(module_name):
        if module_name == "framework.news_observation_pipeline":
            return fake_nop
        if module_name == "framework.indicator_schema":
            return fake_schema_mod
        raise ImportError(module_name)

    monkeypatch.setattr(read_tool, "import_from_repo", fake_import)
    result = read_tool.read_indicator_schema("test-topic")
    assert result["count"] == 0
    assert result["indicators"] == []


# ──────────────────────────────────────────────────────────────────────────
# 3. run_advocate with a FAKE Dream client (no live sidecar)
# ──────────────────────────────────────────────────────────────────────────


class _FakeDream:
    """Records calls and replays a scripted list of responses."""

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


def _long_analysis(*, cite_indicator="t1_indicator_one", cite_evidence="ev_42"):
    """Build an analysis that passes the §4.1 gate: >400 chars + a cited id."""
    body = (
        f"The article reports a concrete shift in observable conditions. "
        f"Indicator {cite_indicator} is the relevant schema entry: its "
        f"likelihood vector favors H1, and the article's quantitative content "
        f"aligns with its threshold direction. Cross-checking recent evidence "
        f"({cite_evidence}) confirms the event is not duplicate coverage. "
        f"The directional alignment holds: the article supports the hypothesis "
        f"the indicator's LR vector points toward, so committing the observation "
        f"is the correct action rather than a wrong-direction update. "
    )
    # Pad to comfortably exceed 400 chars.
    return body + "Detailed multi-paragraph reasoning continues here. " * 6


def test_run_advocate_dispatches_schema_read_then_propose_per_article(monkeypatch):
    """Two articles → one read_indicator_schema + two propose_advocate, then stop."""
    fake = _FakeDream([
        # Turn 1 (forced tool call): read the schema.
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c1", "read_indicator_schema", {"slug": "test-topic"})],
         "usage": {}},
        # Turn 2: propose for article A1.
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c2", "propose_advocate", {
             "article_id": "A1", "verdict": "PARK",
             "proposed_action": {"kind": "PARK"},
             "citation": "the article mentions tankers",
             "analysis": _long_analysis(),
         })],
         "usage": {}},
        # Turn 3: propose for article A2.
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c3", "propose_advocate", {
             "article_id": "A2", "verdict": "WITHDRAW",
             "proposed_action": {"kind": "IGNORE"},
             "citation": "",
             "analysis": _long_analysis(),
         })],
         "usage": {}},
        # Turn 4: stop.
        {"finish_reason": "stop", "content": "advocated on all articles.",
         "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake)

    # Stub read_indicator_schema so no real engine import happens.
    monkeypatch.setitem(TOOLS, "read_indicator_schema", {
        "schema": read_tool.READ_INDICATOR_SCHEMA_SCHEMA,
        "fn": lambda slug, **kw: {"slug": slug, "count": 1,
                                  "indicators": [{"id": "t1_indicator_one", "tier": "tier1_critical"}]},
    })

    articles = [
        {"article_id": "A1", "url": "https://a", "headline": "Tankers fall", "source": "x", "text": "traffic fell 12%"},
        {"article_id": "A2", "url": "https://b", "headline": "Rhetoric only", "source": "y", "text": "officials argued"},
    ]
    result = advocate_agent.run_advocate("test-topic", articles)

    assert result["slug"] == "test-topic"
    assert result["article_ids"] == ["A1", "A2"]
    # Two proposals, one per article.
    assert len(result["proposals"]) == 2
    assert {p["article_id"] for p in result["proposals"]} == {"A1", "A2"}
    # The analysis field is captured in each proposal.
    for p in result["proposals"]:
        assert len(p["analysis"]) > 400
        assert p["analysis_meets_min_len"] is True
    # Trace reflects the 4 turns + the forced first tool call.
    trace = result["trace"]
    assert trace["ok"] is True
    assert trace["turns"] == 4
    assert trace["allowed_tools"] == [
        "read_indicator_schema",
        "read_recent_evidence",
        "propose_advocate",
    ]
    sent_tool_names = [spec["function"]["name"] for spec in fake.calls[0]["kwargs"]["tools"]]
    assert sent_tool_names == trace["allowed_tools"]
    # Turn 1 was forced (tool_choice=required).
    assert fake.calls[0]["kwargs"]["tool_choice"] == "required"
    # Subsequent turns revert to auto.
    assert fake.calls[1]["kwargs"]["tool_choice"] == "auto"
    # The first tool call was read_indicator_schema.
    tool_names = [c["name"] for c in trace["tool_calls"]]
    assert tool_names[0] == "read_indicator_schema"
    # propose_advocate was called once per article.
    assert tool_names.count("propose_advocate") == 2


def test_run_advocate_resets_proposal_store_between_runs(monkeypatch):
    """A second run does not see proposals from the first run."""
    fake1 = _FakeDream([
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c1", "read_indicator_schema", {"slug": "test-topic"})], "usage": {}},
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c2", "propose_advocate", {
             "article_id": "A1", "verdict": "PARK", "proposed_action": {"kind": "PARK"},
             "citation": "", "analysis": _long_analysis()})], "usage": {}},
        {"finish_reason": "stop", "content": "done", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake1)
    monkeypatch.setitem(TOOLS, "read_indicator_schema", {
        "schema": read_tool.READ_INDICATOR_SCHEMA_SCHEMA,
        "fn": lambda slug, **kw: {"slug": slug, "count": 0, "indicators": []},
    })
    advocate_agent.run_advocate("test-topic", [{"article_id": "A1", "url": "u", "headline": "h", "text": "t"}])
    assert len(advocate.list_proposals()) == 1

    # Second run with a fresh fake.
    fake2 = _FakeDream([
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c3", "read_indicator_schema", {"slug": "test-topic"})], "usage": {}},
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c4", "propose_advocate", {
             "article_id": "B1", "verdict": "WITHDRAW", "proposed_action": {"kind": "IGNORE"},
             "citation": "", "analysis": _long_analysis()})], "usage": {}},
        {"finish_reason": "stop", "content": "done", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake2)
    result = advocate_agent.run_advocate("test-topic", [{"article_id": "B1", "url": "u2", "headline": "h2", "text": "t2"}])
    # Only the B1 proposal is present — A1 did not bleed across runs.
    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["article_id"] == "B1"


def test_run_advocate_filters_proposals_to_asked_articles(monkeypatch):
    """A proposal for an article_id we didn't pass in is recorded but not surfaced."""
    fake = _FakeDream([
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c1", "read_indicator_schema", {"slug": "test-topic"})], "usage": {}},
        # Spurious proposal for an unknown article.
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c2", "propose_advocate", {
             "article_id": "Z9", "verdict": "PARK", "proposed_action": {"kind": "PARK"},
             "citation": "", "analysis": _long_analysis()})], "usage": {}},
        # Legitimate proposal for A1.
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c3", "propose_advocate", {
             "article_id": "A1", "verdict": "PARK", "proposed_action": {"kind": "PARK"},
             "citation": "", "analysis": _long_analysis()})], "usage": {}},
        {"finish_reason": "stop", "content": "done", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake)
    monkeypatch.setitem(TOOLS, "read_indicator_schema", {
        "schema": read_tool.READ_INDICATOR_SCHEMA_SCHEMA,
        "fn": lambda slug, **kw: {"slug": slug, "count": 0, "indicators": []},
    })
    result = advocate_agent.run_advocate("test-topic", [{"article_id": "A1", "url": "u", "headline": "h", "text": "t"}])
    # Only A1 is surfaced; Z9 was recorded but filtered out.
    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["article_id"] == "A1"


def test_run_advocate_accepts_url_as_article_id(monkeypatch):
    """DiffusionGemma sometimes uses the URL as the article_id in its tool call.

    The prompt shows both ``[A1]`` and a ``url: https://...`` line per article;
    the model is non-deterministic and may echo either. The harvest must accept
    a proposal whose article_id is the asked article's URL, not just the
    article_id — otherwise a legitimate proposal is silently dropped (the live
    phase-3 test hit this). A genuinely unknown id is still filtered.
    """
    fake = _FakeDream([
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c1", "read_indicator_schema", {"slug": "test-topic"})], "usage": {}},
        # Model emits the URL as article_id instead of "A1".
        {"finish_reason": "tool_calls", "content": "",
         "tool_calls": [_tool_call("c2", "propose_advocate", {
             "article_id": "https://www.example.com/a", "verdict": "PARK",
             "proposed_action": {"kind": "PARK"}, "citation": "", "analysis": _long_analysis()})], "usage": {}},
        {"finish_reason": "stop", "content": "done", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake)
    monkeypatch.setitem(TOOLS, "read_indicator_schema", {
        "schema": read_tool.READ_INDICATOR_SCHEMA_SCHEMA,
        "fn": lambda slug, **kw: {"slug": slug, "count": 0, "indicators": []},
    })
    result = advocate_agent.run_advocate("test-topic", [
        {"article_id": "A1", "url": "https://www.example.com/a", "headline": "h", "text": "t"}
    ])
    # The URL-keyed proposal is surfaced (matched against the asked url).
    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["article_id"] == "https://www.example.com/a"


def test_run_advocate_system_prompt_is_terse_and_imperative():
    """The system prompt must be terse (Phase-1 finding: verbose prompts cause refusal)."""
    sp = advocate_agent.ADVOCATE_SYSTEM_PROMPT
    # Terse: under ~300 chars, no 'you have tools' style abstraction.
    assert len(sp) < 300
    assert "you have tools" not in sp.lower()
    # Imperative: names the actions.
    assert "read" in sp.lower()
    assert "propose_advocate" in sp


# ──────────────────────────────────────────────────────────────────────────
# 4. Live end-to-end (skipped when Dream is down)
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

# The §4.1 phase-3 citation gate: analysis must cite an indicator id
# (t[0-9]_…) or hypothesis id (H[0-9]) or evidence id (ev_NNN).
_CITATION_RE = re.compile(r"(t\d_|H\d|ev_\d+)")


@pytest.mark.live_dream
@pytestmark_live
def test_advocate_live_hormuz_analysis_meets_quality_gate():
    """End-to-end against the real Hormuz topic + real Dream.

    The §4.1 phase-3 gate: every proposal's analysis must be > 400 chars AND
    cite at least one indicator id (t[0-9]_) or hypothesis id (H[0-9]) or
    evidence id (ev_NNN). This is the gate that says "this is actual
    deliberation, not a one-liner restated."
    """
    slug = "calibration-hormuz-reopen-2027"
    # Use a stable, text-heavy article. We pass a short text excerpt so the
    # advocate has something concrete to cite even if the fetch path isn't
    # exercised here (the reading tools pull the real schema + evidence).
    articles = [
        {
            "article_id": "A1",
            "url": "https://www.example.com/hormuz-tanker-traffic",
            "headline": "Tanker traffic through Strait of Hormuz holds near 60% of baseline",
            "source": "example.com",
            "text": (
                "Transit numbers for the Strait of Hormuz remained near 60 percent "
                "of the 2024 baseline this week, according to Lloyd's List data. "
                "Analysts noted no sustained recovery above the 80 percent threshold "
                "that would mark a reopen band. Insurance rates for vessels transiting "
                "the strait stayed elevated, reflecting continued operational risk."
            ),
        },
    ]
    result = advocate_agent.run_advocate(
        slug, articles, max_turns=12, timeout=900.0,
    )
    assert result["proposals"], "advocate recorded no proposals for the articles"
    for p in result["proposals"]:
        assert len(p["analysis"]) > 400, (
            f"analysis for {p['article_id']} is {len(p['analysis'])} chars, "
            f"below the 400-char §4.1 gate"
        )
        assert _CITATION_RE.search(p["analysis"]), (
            f"analysis for {p['article_id']} cites no indicator/hypothesis/evidence id; "
            f"got: {p['analysis'][:200]!r}"
        )
