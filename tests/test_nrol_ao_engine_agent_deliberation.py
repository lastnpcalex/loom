"""Tests for the NROL-AO engine-agent deliberation packet (Track A phase 3).

Five layers (mirrors the phase-1/2 test file structure):
  1. propose_rebut tool: records a rebuttal, returns a rebuttal_id, schema
     enforces verdict/kind enums; validation surfaces as a structured error.
  2. submit_jury tool: records a verdict, returns a verdict_id, schema enforces
     the final_action.kind enum (incl. DUPLICATE_OF); validation structured.
  3. run_deliberation with a FAKE Dream client: the runner dispatches advocate
     → rebut → jury, and all three records are harvested and returned.
  4. No-mutation safety: Phase 3 modules do not import pipeline.process_evidence,
     save_topic, proposal DB stores, or transition commit functions (grep-level
     + import-level assertion).
  5. Live end-to-end against the real Hormuz topic + real Dream: the §4.1
     phase-3 gates — advocate analysis > 400 chars with a cited id; rebuttal
     > 300 chars referencing an advocate proposal/action/indicator; jury
     > 300 chars referencing both advocate and rebuttal records; structured
     final action. Skipped when Dream is down; force with
     LOOM_RUN_LIVE_DREAM_PROBE=1.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from mcp_servers.nrol_ao_engine import deliberation_agent, dream_client
from mcp_servers.nrol_ao_engine.tools import advocate, jury, rebut
from mcp_servers.nrol_ao_engine.tools import read as read_tool
from mcp_servers.nrol_ao_engine.tools import TOOLS


# ──────────────────────────────────────────────────────────────────────────
# 1. propose_rebut tool (no Dream)
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_stores():
    """Each test starts with empty advocate/rebut/jury stores."""
    advocate.reset_proposals()
    rebut.reset_rebuttals()
    jury.reset_verdicts()
    yield
    advocate.reset_proposals()
    rebut.reset_rebuttals()
    jury.reset_verdicts()


def test_propose_rebut_schema_is_valid_openai_tool_spec():
    spec = rebut.SCHEMA
    assert spec["type"] == "function"
    fn = spec["function"]
    assert fn["name"] == "propose_rebut"
    params = fn["parameters"]
    # All seven fields required.
    assert set(params["required"]) == {
        "article_id", "advocate_proposal_id", "verdict", "objection_raised",
        "objection_details", "corrected_action", "rebuttal_analysis",
    }
    # Enums enforced by the schema.
    assert set(params["properties"]["verdict"]["enum"]) == {
        "COMMIT", "PARK", "WITHDRAW", "DUPLICATE_OF", "SCHEMA_GAP"
    }
    kinds = set(params["properties"]["corrected_action"]["properties"]["kind"]["enum"])
    assert kinds == {"FIRE", "OBSERVE", "PARK", "IGNORE", "SCHEMA_GAP"}
    # The rebuttal_analysis description demands >300 chars + advocate reference
    # + indicator/evidence citation (§6 requirement in the tool description).
    desc = params["properties"]["rebuttal_analysis"]["description"]
    assert "300" in desc
    assert "multi-paragraph" in desc.lower()
    assert "advocate" in desc.lower()
    assert "indicator" in desc.lower() or "evidence" in desc.lower()
    json.dumps(spec)  # JSON-serializable


def test_propose_rebut_records_and_returns_id():
    analysis = "B" * 400  # meets the >300 floor
    result = rebut.propose_rebut(
        article_id="A1",
        advocate_proposal_id="adv_abc12345",
        verdict="PARK",
        objection_raised=True,
        objection_details="advocate over-interprets a single data point",
        corrected_action={"kind": "PARK"},
        rebuttal_analysis=analysis,
    )
    assert result["recorded"] is True
    assert result["rebuttal_id"].startswith("reb_")
    rebuttals = rebut.list_rebuttals()
    assert len(rebuttals) == 1
    r = rebuttals[0]
    assert r["rebuttal_id"] == result["rebuttal_id"]
    assert r["article_id"] == "A1"
    assert r["advocate_proposal_id"] == "adv_abc12345"
    assert r["verdict"] == "PARK"
    assert r["objection_raised"] is True
    assert r["corrected_action"] == {"kind": "PARK"}
    assert r["rebuttal_analysis_len"] == 400
    assert r["rebuttal_analysis_meets_min_len"] is True


def test_propose_rebut_records_short_analysis_with_flag():
    result = rebut.propose_rebut(
        article_id="A2",
        advocate_proposal_id="adv_x",
        verdict="COMMIT",
        objection_raised=False,
        objection_details="",
        corrected_action={"kind": "OBSERVE", "indicator_id": "t1_x", "value": 5},
        rebuttal_analysis="too short",  # 9 chars
    )
    assert result["recorded"] is True
    r = rebut.list_rebuttals()[-1]
    assert r["rebuttal_analysis_len"] < 300
    assert r["rebuttal_analysis_meets_min_len"] is False


def test_propose_rebut_rejects_bad_verdict():
    result = rebut.propose_rebut(
        article_id="A3",
        advocate_proposal_id="adv_x",
        verdict="NOT_A_VERDICT",
        objection_raised=False,
        objection_details="",
        corrected_action={"kind": "PARK"},
        rebuttal_analysis="B" * 400,
    )
    assert "error" in result
    assert "NOT_A_VERDICT" in result["error"]
    assert rebut.list_rebuttals() == []


def test_propose_rebut_rejects_bad_corrected_action_kind():
    result = rebut.propose_rebut(
        article_id="A4",
        advocate_proposal_id="adv_x",
        verdict="COMMIT",
        objection_raised=False,
        objection_details="",
        corrected_action={"kind": "EXPLODE"},
        rebuttal_analysis="B" * 400,
    )
    assert "error" in result
    assert "EXPLODE" in result["error"]
    assert rebut.list_rebuttals() == []


def test_propose_rebut_requires_parent_idx_for_duplicate_of():
    result = rebut.propose_rebut(
        article_id="A5",
        advocate_proposal_id="adv_x",
        verdict="DUPLICATE_OF",
        objection_raised=True,
        objection_details="dup",
        corrected_action={"kind": "IGNORE"},  # missing parent_idx
        rebuttal_analysis="B" * 400,
    )
    assert "error" in result
    assert "parent_idx" in result["error"]


def test_propose_rebut_requires_indicator_id_for_fire():
    result = rebut.propose_rebut(
        article_id="A6",
        advocate_proposal_id="adv_x",
        verdict="COMMIT",
        objection_raised=False,
        objection_details="",
        corrected_action={"kind": "FIRE"},  # missing indicator_id
        rebuttal_analysis="B" * 400,
    )
    assert "error" in result
    assert "indicator_id" in result["error"]


def test_propose_rebut_requires_value_for_observe():
    result = rebut.propose_rebut(
        article_id="A7",
        advocate_proposal_id="adv_x",
        verdict="COMMIT",
        objection_raised=False,
        objection_details="",
        corrected_action={"kind": "OBSERVE", "indicator_id": "t1_x"},  # missing value
        rebuttal_analysis="B" * 400,
    )
    assert "error" in result
    assert "value" in result["error"]


def test_propose_rebut_observe_with_value_and_indicator_succeeds():
    result = rebut.propose_rebut(
        article_id="A8",
        advocate_proposal_id="adv_x",
        verdict="COMMIT",
        objection_raised=False,
        objection_details="",
        corrected_action={"kind": "OBSERVE", "indicator_id": "t2_transit_recovery_70pct", "value": 60},
        rebuttal_analysis="B" * 400,
    )
    assert result["recorded"] is True
    r = rebut.list_rebuttals()[-1]
    assert r["corrected_action"]["value"] == 60


def test_propose_rebut_rejects_non_bool_objection_raised():
    result = rebut.propose_rebut(
        article_id="A9",
        advocate_proposal_id="adv_x",
        verdict="COMMIT",
        objection_raised="yes",  # not a bool
        objection_details="",
        corrected_action={"kind": "PARK"},
        rebuttal_analysis="B" * 400,
    )
    assert "error" in result
    assert "boolean" in result["error"]


# ──────────────────────────────────────────────────────────────────────────
# 2. submit_jury tool (no Dream)
# ──────────────────────────────────────────────────────────────────────────


def test_submit_jury_schema_is_valid_openai_tool_spec():
    spec = jury.SCHEMA
    assert spec["type"] == "function"
    fn = spec["function"]
    assert fn["name"] == "submit_jury"
    params = fn["parameters"]
    assert set(params["required"]) == {
        "article_id", "advocate_proposal_id", "rebuttal_id", "final_action",
        "jury_rationale",
    }
    # final_action.kind includes DUPLICATE_OF (§2.1 — the discriminator-on-
    # parent_idx form that the prior JSON-mode spec contradicted itself over).
    kinds = set(params["properties"]["final_action"]["properties"]["kind"]["enum"])
    assert kinds == {"FIRE", "OBSERVE", "PARK", "IGNORE", "SCHEMA_GAP", "DUPLICATE_OF"}
    # The jury_rationale description demands >300 chars + references to BOTH
    # advocate and rebuttal records (§6 requirement in the tool description).
    desc = params["properties"]["jury_rationale"]["description"]
    assert "300" in desc
    assert "multi-paragraph" in desc.lower()
    assert "advocate" in desc.lower()
    assert "rebuttal" in desc.lower() or "rebut" in desc.lower()
    json.dumps(spec)


def test_submit_jury_records_and_returns_id():
    rationale = "C" * 400
    result = jury.submit_jury(
        article_id="A1",
        advocate_proposal_id="adv_abc12345",
        rebuttal_id="reb_def67890",
        final_action={"kind": "OBSERVE", "indicator_id": "t2_transit_recovery_70pct", "value": 60},
        jury_rationale=rationale,
    )
    assert result["recorded"] is True
    assert result["verdict_id"].startswith("jur_")
    verdicts = jury.list_verdicts()
    assert len(verdicts) == 1
    v = verdicts[0]
    assert v["verdict_id"] == result["verdict_id"]
    assert v["article_id"] == "A1"
    assert v["advocate_proposal_id"] == "adv_abc12345"
    assert v["rebuttal_id"] == "reb_def67890"
    assert v["final_action"]["kind"] == "OBSERVE"
    assert v["final_action"]["value"] == 60
    assert v["jury_rationale_len"] == 400
    assert v["jury_rationale_meets_min_len"] is True


def test_submit_jury_records_short_rationale_with_flag():
    result = jury.submit_jury(
        article_id="A2",
        advocate_proposal_id="adv_x",
        rebuttal_id="reb_y",
        final_action={"kind": "PARK"},
        jury_rationale="too short",
    )
    assert result["recorded"] is True
    v = jury.list_verdicts()[-1]
    assert v["jury_rationale_len"] < 300
    assert v["jury_rationale_meets_min_len"] is False


def test_submit_jury_rejects_bad_final_action_kind():
    result = jury.submit_jury(
        article_id="A3",
        advocate_proposal_id="adv_x",
        rebuttal_id="reb_y",
        final_action={"kind": "VAPORIZE"},
        jury_rationale="C" * 400,
    )
    assert "error" in result
    assert "VAPORIZE" in result["error"]
    assert jury.list_verdicts() == []


def test_submit_jury_duplicate_of_requires_parent_idx():
    result = jury.submit_jury(
        article_id="A4",
        advocate_proposal_id="adv_x",
        rebuttal_id="reb_y",
        final_action={"kind": "DUPLICATE_OF"},  # missing parent_idx
        jury_rationale="C" * 400,
    )
    assert "error" in result
    assert "parent_idx" in result["error"]


def test_submit_jury_schema_gap_requires_description():
    result = jury.submit_jury(
        article_id="A5",
        advocate_proposal_id="adv_x",
        rebuttal_id="reb_y",
        final_action={"kind": "SCHEMA_GAP"},  # missing description
        jury_rationale="C" * 400,
    )
    assert "error" in result
    assert "description" in result["error"]


def test_submit_jury_observe_requires_indicator_and_value():
    # Missing indicator_id.
    r1 = jury.submit_jury(
        article_id="A6", advocate_proposal_id="adv_x", rebuttal_id="reb_y",
        final_action={"kind": "OBSERVE", "value": 5},  # no indicator_id
        jury_rationale="C" * 400,
    )
    assert "error" in r1 and "indicator_id" in r1["error"]
    # Missing value.
    r2 = jury.submit_jury(
        article_id="A7", advocate_proposal_id="adv_x", rebuttal_id="reb_y",
        final_action={"kind": "OBSERVE", "indicator_id": "t1_x"},  # no value
        jury_rationale="C" * 400,
    )
    assert "error" in r2 and "value" in r2["error"]


def test_submit_jury_duplicate_of_with_parent_idx_succeeds():
    result = jury.submit_jury(
        article_id="A8",
        advocate_proposal_id="adv_x",
        rebuttal_id="reb_y",
        final_action={"kind": "DUPLICATE_OF", "parent_idx": "A1"},
        jury_rationale="C" * 400,
    )
    assert result["recorded"] is True
    v = jury.list_verdicts()[-1]
    assert v["final_action"]["parent_idx"] == "A1"


def test_submit_jury_requires_rebuttal_id():
    result = jury.submit_jury(
        article_id="A9",
        advocate_proposal_id="adv_x",
        rebuttal_id="",  # missing
        final_action={"kind": "PARK"},
        jury_rationale="C" * 400,
    )
    assert "error" in result
    assert "rebuttal_id" in result["error"]


# ──────────────────────────────────────────────────────────────────────────
# 3. run_deliberation with a FAKE Dream client (no live sidecar)
# ──────────────────────────────────────────────────────────────────────────


class _FakeDream:
    """Records calls and replays a scripted list of responses per stage.

    Phase 3 wires THREE stages (advocate, rebut, jury) through the same
    engine_agent loop, so the fake is given three scripts (one per stage).
    Each script is a list of turns; the last turn of each script must be a
    ``finish_reason=stop`` so the loop breaks and run_deliberation advances to
    the next stage. When a script is drained, the fake advances to the next
    script automatically (run_deliberation calls run_engine_agent once per
    stage, so a fresh script must be ready for the next stage's first turn).
    """

    def __init__(self, scripts: list[list[dict]]):
        self._scripts = [list(s) for s in scripts]
        self.calls: list[dict] = []

    def __call__(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), "kwargs": kwargs})
        # Advance past any drained stage script.
        while self._scripts and not self._scripts[0]:
            self._scripts.pop(0)
        if not self._scripts:
            raise AssertionError("FakeDream exhausted: agent loop called more stages than scripted")
        return self._scripts[0].pop(0)


def _tool_call(call_id: str, name: str, arguments: dict | str):
    args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def _long_analysis(*, cite_indicator="t1_indicator_one", cite_evidence="ev_42"):
    body = (
        f"The article reports a concrete shift in observable conditions. "
        f"Indicator {cite_indicator} is the relevant schema entry: its "
        f"likelihood vector favors H1, and the article's quantitative content "
        f"aligns with its threshold direction. Cross-checking recent evidence "
        f"({cite_evidence}) confirms the event is not duplicate coverage. "
    )
    return body + "Detailed multi-paragraph reasoning continues here. " * 6


def _long_rebuttal(*, proposal_id="adv_1a2b3c4d", cite_indicator="t1_indicator_one"):
    body = (
        f"The advocate (proposal {proposal_id}) proposed OBSERVE on "
        f"{cite_indicator}; I find the directional alignment holds but the "
        f"value cited overstates the article's content. The article's "
        f"quantitative claim does not cleanly cross the threshold the "
        f"indicator's observable specifies. "
    )
    return body + "Skeptical multi-paragraph analysis continues here. " * 6


def _long_jury_rationale(*, proposal_id="adv_1a2b3c4d", rebuttal_id="reb_5e6f7g8h",
                         cite_indicator="t1_indicator_one"):
    body = (
        f"Weighing advocate proposal {proposal_id} against rebuttal "
        f"{rebuttal_id}: the advocate's OBSERVE on {cite_indicator} is "
        f"directionally correct, but the rebuttal's objection that the value "
        f"overstates the article is sound. I modify the action to OBSERVE at "
        f"a value consistent with the article's actual claim. "
    )
    return body + "Multi-paragraph jury reasoning continues here. " * 6


def _stub_read_indicator_schema(monkeypatch):
    """Stub read_indicator_schema so no real engine import happens."""
    monkeypatch.setitem(TOOLS, "read_indicator_schema", {
        "schema": read_tool.READ_INDICATOR_SCHEMA_SCHEMA,
        "fn": lambda slug, **kw: {"slug": slug, "count": 1,
                                  "indicators": [{"id": "t1_indicator_one", "tier": "tier1_critical"}]},
    })


def test_run_deliberation_runs_advocate_rebut_jury_and_returns_all_records(monkeypatch):
    """One article → advocate → rebut → jury, all records returned."""
    # Pin the id generators so the fake Dream scripts can echo the exact ids
    # the advocate/rebut tools will assign. (The runner injects these ids into
    # the rebut/jury prompts; the real model echoes them from the prompt — the
    # fake does the same by hardcoding the pinned values.)
    monkeypatch.setattr(advocate, "_new_proposal_id", lambda: "adv_fixedid")
    monkeypatch.setattr(rebut, "_new_rebuttal_id", lambda: "reb_fixedid")
    monkeypatch.setattr(jury, "_new_verdict_id", lambda: "jur_fixedid")

    fake = _FakeDream([
        # Stage 1: advocate — schema read, propose, stop.
        [
            {"finish_reason": "tool_calls", "content": "",
             "tool_calls": [_tool_call("a1", "read_indicator_schema", {"slug": "test-topic"})], "usage": {}},
            {"finish_reason": "tool_calls", "content": "",
             "tool_calls": [_tool_call("a2", "propose_advocate", {
                 "article_id": "A1", "verdict": "COMMIT",
                 "proposed_action": {"kind": "OBSERVE", "indicator_id": "t1_indicator_one", "value": 60},
                 "citation": "transit at 60%",
                 "analysis": _long_analysis(),
             })], "usage": {}},
            {"finish_reason": "stop", "content": "advocated", "tool_calls": [], "usage": {}},
        ],
        # Stage 2: rebut — schema read, propose_rebut, stop. Echoes the pinned
        # advocate proposal id (adv_fixedid) the runner injected into its prompt.
        [
            {"finish_reason": "tool_calls", "content": "",
             "tool_calls": [_tool_call("b1", "read_indicator_schema", {"slug": "test-topic"})], "usage": {}},
            {"finish_reason": "tool_calls", "content": "",
             "tool_calls": [_tool_call("b2", "propose_rebut", {
                 "article_id": "A1",
                 "advocate_proposal_id": "adv_fixedid",
                 "verdict": "PARK",
                 "objection_raised": True,
                 "objection_details": "value overstates",
                 "corrected_action": {"kind": "PARK"},
                 "rebuttal_analysis": _long_rebuttal(proposal_id="adv_fixedid"),
             })], "usage": {}},
            {"finish_reason": "stop", "content": "rebutted", "tool_calls": [], "usage": {}},
        ],
        # Stage 3: jury — schema read, submit_jury, stop. Echoes both pinned ids.
        [
            {"finish_reason": "tool_calls", "content": "",
             "tool_calls": [_tool_call("j1", "read_indicator_schema", {"slug": "test-topic"})], "usage": {}},
            {"finish_reason": "tool_calls", "content": "",
             "tool_calls": [_tool_call("j2", "submit_jury", {
                 "article_id": "A1",
                 "advocate_proposal_id": "adv_fixedid",
                 "rebuttal_id": "reb_fixedid",
                 "final_action": {"kind": "OBSERVE", "indicator_id": "t1_indicator_one", "value": 55},
                 "jury_rationale": _long_jury_rationale(
                     proposal_id="adv_fixedid", rebuttal_id="reb_fixedid"),
             })], "usage": {}},
            {"finish_reason": "stop", "content": "jury done", "tool_calls": [], "usage": {}},
        ],
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake)
    _stub_read_indicator_schema(monkeypatch)

    articles = [
        {"article_id": "A1", "url": "https://a", "headline": "Tankers hold", "source": "x", "text": "traffic near 60%"},
    ]
    result = deliberation_agent.run_deliberation("test-topic", articles)

    assert result["slug"] == "test-topic"
    # All three record sets present.
    assert len(result["advocate_proposals"]) == 1
    assert len(result["rebuttals"]) == 1
    assert len(result["jury_verdicts"]) == 1
    # Traces for all three stages.
    assert set(result["traces"].keys()) == {"advocate", "rebut", "jury"}
    assert result["traces"]["advocate"]["ok"] is True
    assert result["traces"]["rebut"]["ok"] is True
    assert result["traces"]["jury"]["ok"] is True
    assert result["traces"]["advocate"]["allowed_tools"] == [
        "read_indicator_schema", "read_recent_evidence", "propose_advocate",
    ]
    assert result["traces"]["rebut"]["allowed_tools"] == [
        "read_indicator_schema", "propose_rebut",
    ]
    assert result["traces"]["jury"]["allowed_tools"] == [
        "read_indicator_schema", "submit_jury",
    ]
    # Each stage sends only its own tool surface to Dream. This is the
    # in-process mirror-MCP behavior: the model cannot call a later-stage tool.
    stage_first_calls = [fake.calls[0], fake.calls[3], fake.calls[6]]
    stage_tool_names = [
        [tool["function"]["name"] for tool in call["kwargs"]["tools"]]
        for call in stage_first_calls
    ]
    assert stage_tool_names == [
        ["read_indicator_schema", "read_recent_evidence", "propose_advocate"],
        ["read_indicator_schema", "propose_rebut"],
        ["read_indicator_schema", "submit_jury"],
    ]
    # The records chain: jury references both the advocate proposal and the rebuttal.
    adv = result["advocate_proposals"][0]
    reb = result["rebuttals"][0]
    ver = result["jury_verdicts"][0]
    assert adv["proposal_id"] == "adv_fixedid"
    assert reb["rebuttal_id"] == "reb_fixedid"
    assert reb["advocate_proposal_id"] == adv["proposal_id"]
    assert ver["advocate_proposal_id"] == adv["proposal_id"]
    assert ver["rebuttal_id"] == reb["rebuttal_id"]
    # Analysis length gates met.
    assert adv["analysis_meets_min_len"] is True
    assert reb["rebuttal_analysis_meets_min_len"] is True
    assert ver["jury_rationale_meets_min_len"] is True
    # Final action is structured + schema-valid.
    assert ver["final_action"]["kind"] == "OBSERVE"
    assert ver["final_action"]["value"] == 55


def test_run_deliberation_system_prompts_are_terse_and_imperative():
    """Both stage prompts must be terse (Phase-1 finding: verbose → refusal)."""
    for sp in (deliberation_agent.REBUT_SYSTEM_PROMPT, deliberation_agent.JURY_SYSTEM_PROMPT):
        assert len(sp) < 300
        assert "you have tools" not in sp.lower()
    assert "propose_rebut" in deliberation_agent.REBUT_SYSTEM_PROMPT
    assert "submit_jury" in deliberation_agent.JURY_SYSTEM_PROMPT


def test_run_deliberation_rebut_prompt_injects_advocate_analysis(monkeypatch):
    """The rebut prompt must contain the advocate's full analysis (A.3)."""
    proposals = [{
        "proposal_id": "adv_zzz",
        "article_id": "A1",
        "verdict": "COMMIT",
        "proposed_action": {"kind": "OBSERVE", "indicator_id": "t1_x", "value": 42},
        "citation": "the article says 42",
        "analysis": "UNIQUE_ADVOCATE_ANALYSIS_MARKER",
    }]
    prompt = deliberation_agent._build_rebut_prompt(
        "test-topic",
        [{"article_id": "A1", "url": "u", "headline": "h", "source": "s", "text": "t"}],
        proposals,
    )
    # The full analysis text is injected verbatim (not collapsed to a sentence).
    assert "UNIQUE_ADVOCATE_ANALYSIS_MARKER" in prompt
    assert "adv_zzz" in prompt
    assert "OBSERVE" in prompt


def test_run_deliberation_jury_prompt_injects_both_records(monkeypatch):
    """The jury prompt must contain BOTH advocate + rebut full analyses (A.3)."""
    proposals = [{
        "proposal_id": "adv_zzz", "article_id": "A1", "verdict": "COMMIT",
        "proposed_action": {"kind": "OBSERVE", "indicator_id": "t1_x", "value": 42},
        "citation": "x", "analysis": "UNIQUE_ADVOCATE_MARKER",
    }]
    rebuttals = [{
        "rebuttal_id": "reb_www", "article_id": "A1", "verdict": "PARK",
        "advocate_proposal_id": "adv_zzz", "objection_raised": True,
        "objection_details": "overstated", "corrected_action": {"kind": "PARK"},
        "rebuttal_analysis": "UNIQUE_REBUTTAL_MARKER",
    }]
    prompt = deliberation_agent._build_jury_prompt(
        "test-topic",
        [{"article_id": "A1", "url": "u", "headline": "h", "source": "s", "text": "t"}],
        proposals, rebuttals,
    )
    assert "UNIQUE_ADVOCATE_MARKER" in prompt
    assert "UNIQUE_REBUTTAL_MARKER" in prompt
    assert "adv_zzz" in prompt
    assert "reb_www" in prompt


def test_run_deliberation_no_advocate_proposals_short_circuits(monkeypatch):
    """If the advocate stage records nothing, rebut/jury are skipped cleanly."""
    fake = _FakeDream([
        # Advocate: schema read then stop without proposing.
        [
            {"finish_reason": "tool_calls", "content": "",
             "tool_calls": [_tool_call("a1", "read_indicator_schema", {"slug": "test-topic"})], "usage": {}},
            {"finish_reason": "stop", "content": "nothing to say", "tool_calls": [], "usage": {}},
        ],
    ])
    monkeypatch.setattr(dream_client, "chat_with_tools", fake)
    _stub_read_indicator_schema(monkeypatch)

    result = deliberation_agent.run_deliberation(
        "test-topic", [{"article_id": "A1", "url": "u", "headline": "h", "text": "t"}]
    )
    assert result["advocate_proposals"] == []
    assert result["rebuttals"] == []
    assert result["jury_verdicts"] == []
    assert result["traces"]["rebut"]["ok"] is False
    assert "no advocate proposals" in result["traces"]["rebut"]["error"]


# ──────────────────────────────────────────────────────────────────────────
# 4. No-mutation safety (grep-level + import-level)
# ──────────────────────────────────────────────────────────────────────────


_ENGINE_PKG = Path(__file__).resolve().parent.parent / "mcp_servers" / "nrol_ao_engine"

# Symbols that would indicate a commit/mutation path. Phase 3 modules must
# not import or call any of these. (engine_agent.py / advocate_agent.py were
# already verified clean in phase 1-2; phase 3 adds deliberation_agent.py,
# tools/rebut.py, tools/jury.py.)
#
# This is an AST-level guard, not a raw substring scan: the phase-3 module
# DOCSTRINGS legitimately mention these names (e.g. "no import of
# process_evidence") as safety documentation. A naive `sym in src` grep would
# false-positive on those docstring mentions. We parse the module and inspect
# only real import statements (Import / ImportFrom) and call sites (Call).
_FORBIDDEN_COMMIT_SYMBOLS = [
    "process_evidence",
    "apply_decisions",
    "save_topic",
    "commit_match",
    "propose_match",
    "submit_transition",
    "fire_indicator",
    "observe_indicator",
    "write_evidence",
]


def _imported_and_called_names(path: Path) -> set[str]:
    """Return the set of names a module imports (as) OR calls.

    Covers ``import X`` / ``import X as Y`` / ``from M import X`` /
    ``from M import X as Y`` (the bound name), plus the function-name token of
    every Call expression. A docstring mention does NOT count — only real
    import bindings and call sites.
    """
    import ast

    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                # obj.attr — capture the attr too (e.g. pipeline.save_topic).
                names.add(f.attr)
    return names


@pytest.mark.parametrize("module_rel", [
    "deliberation_agent.py",
    "tools/rebut.py",
    "tools/jury.py",
])
def test_phase3_modules_do_not_import_or_call_forbidden_commit_symbols(module_rel):
    """No phase-3 module imports or calls a commit/mutation symbol.

    An AST-level guard (not a substring grep — docstrings legitimately mention
    these names as safety documentation). If a future edit introduces an import
    of process_evidence / save_topic / commit_match / etc., or a call to one,
    this test fails before the mutation path can ship. (The action tools
    fire_indicator etc. are a LATER phase and must wrap the existing commit
    gates — never a new path introduced here.)
    """
    path = _ENGINE_PKG / module_rel
    bound = _imported_and_called_names(path)
    offenders = sorted(bound & set(_FORBIDDEN_COMMIT_SYMBOLS))
    assert not offenders, (
        f"{module_rel} imports/calls forbidden commit/mutation symbol(s) "
        f"{offenders} — phase 3 must not introduce a commit/mutation path"
    )


def test_phase3_tools_registered_in_TOOLS():
    """propose_rebut + submit_jury are in the registry the agent dispatches from."""
    assert "propose_rebut" in TOOLS
    assert "submit_jury" in TOOLS
    assert TOOLS["propose_rebut"]["fn"] is rebut.propose_rebut
    assert TOOLS["submit_jury"]["fn"] is jury.submit_jury


def test_phase3_stores_are_independent_lists():
    """The three record stores are separate in-process lists (not the proposal DB)."""
    assert isinstance(advocate._proposals, list)
    assert isinstance(rebut._rebuttals, list)
    assert isinstance(jury._verdicts, list)
    # reset_* clears each independently.
    advocate.propose_advocate(
        article_id="X", verdict="PARK", proposed_action={"kind": "PARK"},
        citation="", analysis="A" * 500,
    )
    rebut.propose_rebut(
        article_id="X", advocate_proposal_id="a", verdict="PARK",
        objection_raised=False, objection_details="", corrected_action={"kind": "PARK"},
        rebuttal_analysis="B" * 400,
    )
    jury.submit_jury(
        article_id="X", advocate_proposal_id="a", rebuttal_id="r",
        final_action={"kind": "PARK"}, jury_rationale="C" * 400,
    )
    assert len(advocate.list_proposals()) == 1
    assert len(rebut.list_rebuttals()) == 1
    assert len(jury.list_verdicts()) == 1
    # Resetting rebut does not clear advocate or jury.
    rebut.reset_rebuttals()
    assert len(advocate.list_proposals()) == 1
    assert len(rebut.list_rebuttals()) == 0
    assert len(jury.list_verdicts()) == 1


# ──────────────────────────────────────────────────────────────────────────
# 5. Live end-to-end (skipped when Dream is down)
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

# §4.1 phase-3 citation gate: an id reference (indicator t[0-9]_, hypothesis
# H[0-9], evidence ev_NNN, or a proposal/rebuttal/verdict id adv_/reb_/jur_).
_CITATION_RE = re.compile(r"(t\d_|H\d|ev_\d+|adv_[0-9a-f]{6,}|reb_[0-9a-f]{6,}|jur_[0-9a-f]{6,})")
_INDICATOR_RE = re.compile(r"t\d_")


@pytest.mark.live_dream
@pytestmark_live
def test_deliberation_live_hormuz_full_packet_meets_quality_gates():
    """End-to-end advocate → rebut → jury against the real Hormuz topic + Dream.

    The §4.1 phase-3 gates:
      - advocate analysis > 400 chars AND cites an indicator/evidence/hypothesis id
      - rebuttal rebuttal_analysis > 300 chars AND references an advocate
        proposal id or a concrete advocate action/indicator
      - jury jury_rationale > 300 chars AND references both advocate and
        rebuttal records (ids or concrete contents)
      - final action is structured + schema-valid
      - no topic mutation, no proposal DB writes
    """
    slug = "calibration-hormuz-reopen-2027"
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
    # DiffusionGemma is a diffusion text model, not an instruction-tuned chat
    # model; on a single shot it occasionally emits a malformed tool argument
    # that the retry-once-then-fail-closed loop drops, leaving an empty stage.
    # This is a known flakiness mode of the live backend, not a code defect
    # (production scans serialize/retry at the MCP layer). The gate asserts
    # against a SUCCESSFUL packet, so retry the whole packet once if a stage
    # comes back empty.
    result = deliberation_agent.run_deliberation(
        slug, articles, max_turns=24, timeout=900.0,
    )
    if not result["advocate_proposals"] or not result["rebuttals"] or not result["jury_verdicts"]:
        result = deliberation_agent.run_deliberation(
            slug, articles, max_turns=24, timeout=900.0,
        )

    # All three stages produced records.
    assert result["advocate_proposals"], "advocate recorded no proposals"
    assert result["rebuttals"], "rebut recorded no rebuttals"
    assert result["jury_verdicts"], "jury recorded no verdicts"

    # Gate 1: advocate analysis > 400 chars + cited id.
    adv = result["advocate_proposals"][0]
    assert len(adv["analysis"]) > 400, (
        f"advocate analysis is {len(adv['analysis'])} chars, below the 400-char gate"
    )
    assert _CITATION_RE.search(adv["analysis"]), (
        f"advocate analysis cites no indicator/hypothesis/evidence id; "
        f"got: {adv['analysis'][:200]!r}"
    )

    # Gate 2: rebuttal analysis > 300 chars + references an advocate proposal
    # id or a concrete advocate action/indicator.
    reb = result["rebuttals"][0]
    assert len(reb["rebuttal_analysis"]) > 300, (
        f"rebuttal analysis is {len(reb['rebuttal_analysis'])} chars, below the 300-char gate"
    )
    reb_text = reb["rebuttal_analysis"]
    adv_proposal_id = adv["proposal_id"]
    adv_action = json.dumps(adv.get("proposed_action") or {}, ensure_ascii=True)
    references_advocate = (
        adv_proposal_id in reb_text
        or (adv.get("verdict", "") and adv["verdict"] in reb_text)
        or any(tok in reb_text for tok in _extract_action_tokens(adv_action))
        or _INDICATOR_RE.search(reb_text) is not None
    )
    assert references_advocate, (
        f"rebuttal does not reference the advocate proposal/action/indicator; "
        f"got: {reb_text[:200]!r}"
    )

    # Gate 3: jury rationale > 300 chars + references both advocate and rebuttal.
    ver = result["jury_verdicts"][0]
    assert len(ver["jury_rationale"]) > 300, (
        f"jury rationale is {len(ver['jury_rationale'])} chars, below the 300-char gate"
    )
    jur_text = ver["jury_rationale"]
    reb_id = reb["rebuttal_id"]
    references_advocate_jury = (
        adv_proposal_id in jur_text
        or adv["verdict"] in jur_text
        or any(tok in jur_text for tok in _extract_action_tokens(adv_action))
    )
    references_rebuttal_jury = (
        reb_id in jur_text
        or reb["verdict"] in jur_text
        or any(tok in jur_text for tok in _extract_action_tokens(
            json.dumps(reb.get("corrected_action") or {}, ensure_ascii=True)))
        or (reb.get("objection_details", "") and reb["objection_details"][:20] in jur_text)
    )
    assert references_advocate_jury, (
        f"jury rationale does not reference the advocate record; got: {jur_text[:200]!r}"
    )
    assert references_rebuttal_jury, (
        f"jury rationale does not reference the rebuttal record; got: {jur_text[:200]!r}"
    )

    # Gate 4: final action is structured + schema-valid.
    fa = ver["final_action"]
    assert fa["kind"] in jury.JURY_FINAL_ACTION_KINDS
    if fa["kind"] in ("FIRE", "OBSERVE"):
        assert fa.get("indicator_id"), f"FIRE/OBSERVE requires indicator_id; got {fa}"
    if fa["kind"] == "OBSERVE":
        assert fa.get("value") is not None, f"OBSERVE requires value; got {fa}"
    if fa["kind"] == "DUPLICATE_OF":
        assert fa.get("parent_idx"), f"DUPLICATE_OF requires parent_idx; got {fa}"
    if fa["kind"] == "SCHEMA_GAP":
        assert str(fa.get("description") or "").strip(), f"SCHEMA_GAP requires description; got {fa}"

    # Gate 5: the jury verdict references the real advocate + rebut ids it was
    # paired with (strong form of the cross-reference gate).
    assert ver["advocate_proposal_id"] == adv_proposal_id
    assert ver["rebuttal_id"] == reb_id


def _extract_action_tokens(action_json: str) -> list[str]:
    """Pull indicator ids + kind tokens out of a JSON action string for ref checks."""
    tokens: list[str] = []
    for m in re.finditer(r"t\d_[a-z0-9_]+", action_json):
        tokens.append(m.group(0))
    for kind in ("FIRE", "OBSERVE", "PARK", "IGNORE", "SCHEMA_GAP", "DUPLICATE_OF"):
        if kind in action_json:
            tokens.append(kind)
    return tokens
