"""Capability tests for the NROL-AO MCP boundary.

These are the executable form of the system's core promise: beliefs only
move when the world moves — through typed transitions bound to
pre-committed likelihoods. No operator, human or LLM, can submit a
posterior through this surface.

The suite builds an isolated fixture repo (engine + framework copied from
the source repo, synthetic topic state) so no live topic data is touched.
"""

import json
import os
import subprocess
import shutil
import sys
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

SOURCE_REPO = Path(os.environ.get("NROL_AO_SOURCE_REPO", r"C:\Claude-Code\NROL-AO\temp-repo"))

pytestmark = pytest.mark.skipif(
    not (SOURCE_REPO / "engine.py").is_file(),
    reason="NROL-AO engine repo not available at NROL_AO_SOURCE_REPO",
)

SLUG = "test-capability"


def _fixture_topic() -> dict:
    return {
        "meta": {
            "slug": SLUG,
            "title": "Capability test topic",
            "question": "Which synthetic outcome resolves by 2030-12-31?",
            "resolution": "Resolves to the hypothesis whose synthetic event is published by test-rig before 2030-12-31.",
            "resolutionDate": "2030-12-31",
            "classification": "CALIBRATION",
            "status": "ACTIVE",
            "lens": "OPERATOR_JUDGMENT",
            "calibrationStatus": "SKIPPED_OPERATOR_JUDGMENT",
            "calibrationSkipReason": "Synthetic capability-test fixture; no real-world claim.",
            "created": "2026-06-01T00:00:00+00:00",
            "startDate": "2026-06-01",
            "dayCount": 1,
            "lastUpdated": "2026-06-01T00:00:00+00:00",
            "lastScanned": "2026-06-01T00:00:00+00:00",
        },
        "model": {
            "hypotheses": {
                # midpoint is required: bayesian_update recomputes
                # model.expectedValue as sum(midpoint * posterior).
                "H1": {"label": "Synthetic outcome A by 2030", "midpoint": 100, "posterior": 0.5},
                "H2": {"label": "Synthetic outcome B by 2030", "midpoint": 50, "posterior": 0.3},
                "H3": {"label": "Neither outcome by 2030", "midpoint": 0, "posterior": 0.2},
            },
            # Initial history entry documents the non-uniform priors so the
            # design gate ("NON-UNIFORM PRIORS without justification") passes.
            "posteriorHistory": [
                {
                    "date": "2026-06-01",
                    "timestamp": "2026-06-01T00:00:00+00:00",
                    "posteriors": {"H1": 0.5, "H2": 0.3, "H3": 0.2},
                    "note": (
                        "Initial priors: synthetic fixture; H1 favored by design "
                        "so directional assertions have headroom."
                    ),
                }
            ],
        },
        "governance": {
            "health": "HEALTHY",
            "issues": [],
            "flagged_for_indicator_review": [],
            "flagged_schema_gaps": [],
            "proposed_schema_extensions": [],
        },
        "indicators": {
            "tiers": {
                "tier1_critical": [
                    {
                        "id": "ind_binary_mild",
                        "desc": "Synthetic binary indicator: test-rig publishes event A confirmation.",
                        "status": "NOT_FIRED",
                        "firedDate": None,
                        "note": None,
                        "posteriorEffect": "H1 +6pp; H2 -3pp; H3 -3pp.",
                        "likelihoods": {"H1": 0.6, "H2": 0.45, "H3": 0.5},
                        "lr_decay": 1.0,
                        "n_firings": 0,
                        "resolution_class": False,
                        "shape": "per_event_member",
                        "causal_event_id": "test_event_binary",
                    },
                    {
                        "id": "ind_observable_metric",
                        "desc": "Synthetic numeric indicator: monthly test metric percent from test-rig.",
                        "status": "NOT_FIRED",
                        "firedDate": None,
                        "note": None,
                        "posteriorEffect": "H1 +5pp; H2 -2pp; H3 -3pp.",
                        "likelihoods": {"H1": 0.65, "H2": 0.45, "H3": 0.35},
                        "lr_decay": 1.0,
                        "n_firings": 0,
                        "resolution_class": False,
                        "shape": "per_event_member",
                        "causal_event_id": "test_event_metric",
                        "observable": {
                            "metric": "test:metric_pct",
                            "family": "logistic",
                            "threshold_value": 50,
                            "baseline": 40,
                            "direction": "higher_strengthens",
                        },
                    },
                ],
                "tier2_strong": [],
                "tier3_suggestive": [],
            },
            "anti_indicators": [],
        },
        "actorModel": {},
        "dataFeeds": {},
        "predictionScoring": {},
        "contradictionTracker": {},
        "evidenceLog": [],
        "sourceCalibration": {},
    }


@pytest.fixture(scope="session")
def nrol_repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("nrol_fixture_repo")
    shutil.copy2(SOURCE_REPO / "engine.py", root / "engine.py")
    shutil.copy2(SOURCE_REPO / "governor.py", root / "governor.py")
    # export_blackhole_snapshot lives at the repo root; publish_black_hole_snapshot
    # imports it via _import_from_repo.
    if (SOURCE_REPO / "export_blackhole_snapshot.py").is_file():
        shutil.copy2(SOURCE_REPO / "export_blackhole_snapshot.py",
                     root / "export_blackhole_snapshot.py")
    shutil.copytree(
        SOURCE_REPO / "framework",
        root / "framework",
        ignore=shutil.ignore_patterns("__pycache__", "backtest_data"),
    )
    if (SOURCE_REPO / "sources").is_dir():
        shutil.copytree(SOURCE_REPO / "sources", root / "sources")
    (root / "topics").mkdir()
    (root / "loom" / "topics").mkdir(parents=True)
    (root / "briefs").mkdir()
    return root


@pytest.fixture(scope="session")
def nrol(nrol_repo):
    """Configure env and return the MCP server module bound to the fixture repo."""
    os.environ["NROL_AO_REPO"] = str(nrol_repo)
    os.environ["NROL_AO_ACTIVITY_DIR"] = str(nrol_repo / "mcp_activity")
    os.environ.pop("LOOM_CONV_ID", None)
    # Commits are fail-closed without Loom context; tests opt out explicitly.
    os.environ["NROL_AO_ALLOW_UNGATED_COMMITS"] = "1"
    from mcp_servers.nrol_ao import server

    return server


@pytest.fixture
def topic_path(nrol, nrol_repo):
    """Write a pristine synthetic topic before each test."""
    path = nrol_repo / "topics" / f"{SLUG}.json"
    path.write_text(json.dumps(_fixture_topic(), indent=2), encoding="utf-8")
    return path


def _disk_topic(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _disk_posteriors(path: Path) -> dict:
    return {k: v["posterior"] for k, v in _disk_topic(path)["model"]["hypotheses"].items()}


def _evidence(text: str = "", **extra) -> dict:
    return {
        "text": text or f"Synthetic observation {uuid.uuid4().hex[:8]} from test-rig.",
        "source": "test-rig",
        "tag": "EVENT",
        **extra,
    }


def _submit(nrol, **kwargs) -> dict:
    # Capability tests probe the engine/gate machinery directly; the
    # deliberation gate is waived by default here and exercised explicitly
    # in the deliberation-gate section below.
    kwargs.setdefault("no_deliberation_reason", "capability test")
    return json.loads(nrol.submit_transition(**kwargs))


def _seed_coverage_indicators(topic_path: Path) -> None:
    """Add tier1 observable indicators favoring H2 and H3 to the fixture topic.

    The fixture's two tier1 indicators both favor H1, so the set-level
    directional-coverage lint (each H needs an observable indicator whose LR
    vector favors it) blocks any new-indicator add. Seeding H2/H3 coverage lets
    anti-indicator apply tests isolate the inversion check from the coverage
    check. Written via direct topic-JSON edit (test setup, not the MCP path).
    """
    topic = _disk_topic(topic_path)
    tier1 = topic["indicators"]["tiers"]["tier1_critical"]
    existing_ids = {i["id"] for i in tier1}
    for h, evid in (("H2", "test_cov_h2"), ("H3", "test_cov_h3")):
        cid = f"ind_cov_{h.lower()}"
        if cid in existing_ids:
            continue
        lrs = {"H1": 0.35, "H2": 0.4, "H3": 0.4}
        lrs[h] = 0.6  # favor the target H so coverage_toward[H] >= 1
        tier1.append({
            "id": cid, "desc": f"coverage for {h}", "status": "NOT_FIRED",
            "firedDate": None, "note": None, "posteriorEffect": f"{h} up",
            "likelihoods": lrs, "lr_decay": 1.0, "n_firings": 0,
            "resolution_class": False, "shape": "per_event_member",
            "causal_event_id": evid,
            "observable": {"metric": f"test:cov_{h.lower()}", "family": "logistic",
                           "threshold_value": 0.5, "baseline": 0.1,
                           "direction": "higher_strengthens"},
        })
    topic_path.write_text(json.dumps(topic, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Read-path robustness
# ---------------------------------------------------------------------------


def test_topic_status_lists_fixture(nrol, topic_path):
    out = json.loads(nrol.topic_status())
    assert "error" not in out
    assert SLUG in [r["slug"] for r in out["topics"]]


def test_topic_status_lists_bom_prefixed_topic(nrol, topic_path):
    raw = topic_path.read_text(encoding="utf-8")
    topic_path.write_text("\ufeff" + raw, encoding="utf-8")

    out = json.loads(nrol.topic_status())

    assert "error" not in out
    assert SLUG in [r["slug"] for r in out["topics"]]


def test_read_topic_exposes_search_queries(nrol, topic_path):
    topic = _disk_topic(topic_path)
    topic["searchQueries"] = ["synthetic event Reuters official report"]
    topic_path.write_text(json.dumps(topic, indent=2), encoding="utf-8")

    out = json.loads(nrol.read_topic(SLUG, include_indicators=False, evidence_limit=0))

    assert out["searchQueries"] == ["synthetic event Reuters official report"]


def test_search_query_update_lifecycle(nrol, topic_path, monkeypatch):
    monkeypatch.setattr(
        nrol.llama_client,
        "chat",
        lambda *a, **k: {
            "text": (
                "VERDICT: APPROVE\n"
                "COVERAGE: covers event, source, recovery, failure, and measurement axes\n"
                "NEUTRALITY: balanced across outcomes\n"
                "OVERFITTING: none\n"
                "NOISE: acceptable\n"
                "SCHEMA_AXIS: covers the metric observable and event indicators\n"
                "RECOMMENDATION: apply\n"
            ),
            "model": "test-local-llama",
            "finish_reason": "stop",
        },
    )
    add = [
        "synthetic event Reuters official report",
        "synthetic failure risk indicator signal",
        "synthetic agreement talks recovery confirmed",
        "synthetic metric percent baseline data",
    ]

    proposed = json.loads(nrol.propose_search_query_update(
        slug=SLUG,
        add=add,
        remove=[],
        rationale="Initial durable query coverage for synthetic topic.",
        coverage_gaps=[
            "missing source query",
            "missing failure and recovery axes",
            "missing metric query",
        ],
    ))
    assert "error" not in proposed, proposed
    proposal_id = proposed["id"]
    assert proposed["status"] == "pending"

    listed = json.loads(nrol.list_search_query_updates(slug=SLUG))
    assert proposal_id in [p["id"] for p in listed["proposals"]]

    blocked = json.loads(nrol.apply_search_query_update(proposal_id, dry_run=True))
    assert "requires red_team verdict APPROVE" in blocked["error"]

    reviewed = json.loads(nrol.red_team_search_query_update(proposal_id))
    assert reviewed["red_team"]["verdict"] == "APPROVE", reviewed
    assert reviewed["red_team"]["model"] == "test-local-llama"
    assert reviewed["red_team"]["deterministic"]["verdict"] == "CHECK"

    preview = json.loads(nrol.apply_search_query_update(proposal_id, dry_run=True))
    assert preview["committed"] is False
    assert preview["after"] == add

    applied = json.loads(nrol.apply_search_query_update(proposal_id, dry_run=False))
    assert "error" not in applied, applied
    assert applied["committed"] is True
    assert applied["after"] == add

    topic = _disk_topic(topic_path)
    assert topic["searchQueries"] == add
    history = topic["governance"]["search_query_history"]
    assert history[-1]["proposal_id"] == proposal_id

    final_queue = json.loads(nrol.list_search_query_updates(slug=SLUG, status="applied"))
    assert proposal_id in [p["id"] for p in final_queue["proposals"]]


def test_search_query_update_rejects_site_only_set(nrol, topic_path):
    proposed = json.loads(nrol.propose_search_query_update(
        slug=SLUG,
        add=[
            "synthetic event site:example.com",
            "synthetic metric site:example.org",
            "synthetic report site:example.net",
        ],
        rationale="Bad proposal: all queries are site-filtered.",
    ))

    assert proposed["error"] == "search query proposal failed validation"
    assert "all queries are site-filtered" in " ".join(proposed["validation"]["errors"])


def test_search_query_update_requires_red_team_approve(nrol, topic_path, monkeypatch):
    monkeypatch.setattr(
        nrol.llama_client,
        "chat",
        lambda *a, **k: {
            "text": (
                "VERDICT: REVISE\n"
                "COVERAGE: too thin\n"
                "NEUTRALITY: acceptable\n"
                "OVERFITTING: none\n"
                "NOISE: acceptable\n"
                "SCHEMA_AXIS: weak\n"
                "RECOMMENDATION: add source and measurement coverage\n"
            ),
            "model": "test-local-llama",
            "finish_reason": "stop",
        },
    )
    proposed = json.loads(nrol.propose_search_query_update(
        slug=SLUG,
        add=["synthetic event official report"],
        rationale="Weak proposal missing recovery/measurement/source breadth.",
        coverage_gaps=["missing durable query coverage"],
    ))
    assert "error" not in proposed, proposed
    proposal_id = proposed["id"]

    reviewed = json.loads(nrol.red_team_search_query_update(proposal_id))
    assert reviewed["red_team"]["verdict"] in {"REVISE", "REJECT"}

    applied = json.loads(nrol.apply_search_query_update(proposal_id, dry_run=False))
    assert "requires red_team verdict APPROVE" in applied["error"]

    withdrawn = json.loads(nrol.withdraw_search_query_update(
        proposal_id, reason="red-team requested broader query coverage"
    ))
    assert withdrawn["status"] == "withdrawn"


def test_nrol_status_reports_effective_topic_state_paths(nrol, nrol_repo, topic_path):
    out = json.loads(nrol.nrol_status())
    assert "error" not in out
    assert out["repo"] == str(nrol_repo)
    assert out["state_root"] == str(nrol_repo)
    assert out["topics_dir"] == str(nrol_repo / "topics")
    assert out["topics"] >= 1


def test_malformed_file_does_not_break_listing(nrol, nrol_repo, topic_path):
    bad = nrol_repo / "topics" / "manifest.json"
    bad.write_text(json.dumps({"topics": [SLUG]}), encoding="utf-8")
    try:
        out = json.loads(nrol.topic_status())
        assert "error" not in out
        assert SLUG in [r["slug"] for r in out["topics"]]
        assert "manifest" in [s["slug"] for s in out.get("skipped", [])]
        listed = json.loads(nrol.list_topics())
        assert SLUG in [r["slug"] for r in listed]
        assert "manifest" not in [r["slug"] for r in listed]
    finally:
        bad.unlink()


# ---------------------------------------------------------------------------
# Rejection gates: malformed transitions must not move anything
# ---------------------------------------------------------------------------


def test_fire_requires_indicator_id(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(nrol, slug=SLUG, transition="FIRE", evidence=_evidence(), commit=True)
    assert "error" in out
    assert "indicator_id" in out["error"]
    assert _disk_posteriors(topic_path) == before


def test_fire_unknown_indicator_rejected(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_does_not_exist", commit=True,
    )
    assert "error" in out
    assert "not found" in out["error"]
    assert _disk_posteriors(topic_path) == before


def test_observe_requires_value(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(
        nrol, slug=SLUG, transition="OBSERVE", evidence=_evidence(),
        indicator_id="ind_observable_metric", commit=True,
    )
    assert "error" in out
    assert "observed_value" in out["error"]
    assert _disk_posteriors(topic_path) == before


def test_observe_requires_observable_block(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(
        nrol, slug=SLUG, transition="OBSERVE", evidence=_evidence(),
        indicator_id="ind_binary_mild", observed_value=55.0, commit=True,
    )
    assert "error" in out
    assert "observable" in out["error"]
    assert _disk_posteriors(topic_path) == before


def test_unknown_transition_rejected(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(nrol, slug=SLUG, transition="SET_POSTERIOR", evidence=_evidence(), commit=True)
    assert "error" in out
    assert _disk_posteriors(topic_path) == before


def test_evidence_cannot_smuggle_likelihoods(nrol, topic_path):
    """The evidence whitelist must drop likelihood/posterior payloads."""
    evidence = _evidence(
        likelihoods={"H1": 0.99, "H2": 0.01, "H3": 0.01},
        posteriors={"H1": 0.9},
        target_posterior=0.9,
    )
    out = _submit(nrol, slug=SLUG, transition="PARK", evidence=evidence, commit=False)
    entry = out["evidence_entry"]
    assert "likelihoods" not in entry
    assert "posteriors" not in entry
    assert "target_posterior" not in entry


# ---------------------------------------------------------------------------
# Non-mutating transitions
# ---------------------------------------------------------------------------


def test_dry_run_never_mutates(nrol, topic_path):
    before_bytes = topic_path.read_bytes()
    out = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=False,
    )
    assert out["committed"] is False
    assert out["posteriors_before"] == out["posteriors_after"]
    assert out["pre_committed_likelihoods"] == {"H1": 0.6, "H2": 0.45, "H3": 0.5}
    assert topic_path.read_bytes() == before_bytes


def test_ignore_writes_nothing(nrol, topic_path):
    before_bytes = topic_path.read_bytes()
    out = _submit(nrol, slug=SLUG, transition="IGNORE", evidence=_evidence(), commit=True)
    assert out["ignored"] is True
    assert out["committed"] is False
    assert topic_path.read_bytes() == before_bytes


def test_park_commits_evidence_without_posterior_movement(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(nrol, slug=SLUG, transition="PARK", evidence=_evidence(), commit=True)
    assert out.get("committed") is True, out
    after_topic = _disk_topic(topic_path)
    after = {k: v["posterior"] for k, v in after_topic["model"]["hypotheses"].items()}
    assert after == before
    assert len(after_topic["evidenceLog"]) == 1
    assert len(after_topic["governance"]["flagged_for_indicator_review"]) == 1


def test_schema_gap_commits_without_posterior_movement(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(
        nrol, slug=SLUG, transition="SCHEMA_GAP", evidence=_evidence(),
        reason="no indicator covers synthetic direction", missing_direction="H2-negative",
        commit=True,
    )
    assert out.get("committed") is True, out
    after_topic = _disk_topic(topic_path)
    after = {k: v["posterior"] for k, v in after_topic["model"]["hypotheses"].items()}
    assert after == before
    assert len(after_topic["governance"]["flagged_schema_gaps"]) == 1


# ---------------------------------------------------------------------------
# Mutating transitions: Bayes through pre-committed likelihoods only
# ---------------------------------------------------------------------------


def test_fire_moves_posteriors_via_precommitted_lrs(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=True,
    )
    assert out.get("committed") is True, out
    after = _disk_posteriors(topic_path)
    assert after != before
    # engine rounds posteriors to 4 decimals; sum holds to ~5e-4
    assert abs(sum(after.values()) - 1.0) < 5e-4
    # H1 has the highest pre-committed likelihood; it must not lose mass.
    assert after["H1"] > before["H1"]


def test_repeat_fire_attenuates(nrol, topic_path):
    """Re-firing the same indicator must move posteriors less than the first
    firing (lr_decay repeat-firing attenuation + causal de-correlation)."""
    p0 = _disk_posteriors(topic_path)
    out1 = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=True,
    )
    assert out1.get("committed") is True, out1
    p1 = _disk_posteriors(topic_path)
    out2 = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=True,
    )
    assert out2.get("committed") is True, out2
    p2 = _disk_posteriors(topic_path)
    first_shift = abs(p1["H1"] - p0["H1"])
    second_shift = abs(p2["H1"] - p1["H1"])
    assert first_shift > 0
    assert second_shift < first_shift


def test_observe_applies_mechanical_partial_lr(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(
        nrol, slug=SLUG, transition="OBSERVE", evidence=_evidence(),
        indicator_id="ind_observable_metric", observed_value=55.0, commit=True,
    )
    assert out.get("committed") is True, out
    after = _disk_posteriors(topic_path)
    assert after != before
    # engine rounds posteriors to 4 decimals; sum holds to ~5e-4
    assert abs(sum(after.values()) - 1.0) < 5e-4
    # 55 is beyond the higher_strengthens threshold (50): H1 (highest
    # likelihood) must not lose mass.
    assert after["H1"] > before["H1"]


def test_legacy_posterior_path_rejected_on_active_topic(nrol, topic_path, monkeypatch):
    """The legacy run_update(posteriors=...) freeform path must refuse ACTIVE
    topics — the migration spec's 'biggest authority leak'. Admin override
    requires a signed NROL_AO_ADMIN_POSTERIORS_REASON."""
    import importlib

    monkeypatch.delenv("NROL_AO_ADMIN_POSTERIORS_REASON", raising=False)
    update_mod = importlib.import_module("framework.update")
    before = _disk_posteriors(topic_path)
    with pytest.raises(Exception) as exc_info:
        update_mod.run_update(
            SLUG,
            posteriors={"H1": 0.9, "H2": 0.05, "H3": 0.05},
            posterior_reason="operator says so",
        )
    assert "ACTIVE" in str(exc_info.value)
    assert _disk_posteriors(topic_path) == before


# ---------------------------------------------------------------------------
# Proposal lifecycle: submit_article -> propose_match -> commit_match
# ---------------------------------------------------------------------------


def _article(**extra) -> dict:
    suffix = uuid.uuid4().hex[:8]
    return {
        "headline": f"Synthetic event report {suffix}",
        "url": f"https://example.test/articles/{suffix}",
        "source": "test-wire",
        "date": "2026-06-09",
        "body": "Official synthetic print released by test-rig.",
        **extra,
    }


def test_submit_article_dedupes_by_url(nrol, topic_path):
    art = _article()
    first = json.loads(nrol.submit_article(art))
    again = json.loads(nrol.submit_article(art))
    assert "error" not in first
    assert first["id"].startswith("art-")
    assert again["id"] == first["id"]
    assert again["deduped"] is True


def test_propose_match_validates_without_mutation(nrol, topic_path):
    art = json.loads(nrol.submit_article(_article()))
    before_bytes = topic_path.read_bytes()

    bad_action = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="IGNORE", rationale="meh"))
    assert "error" in bad_action

    bad_indicator = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="FIRE",
        indicator_id="ind_nope", rationale="directional case",
        no_deliberation_reason="capability test"))
    assert "error" in bad_indicator

    no_rationale = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="PARK", rationale=""))
    assert "error" in no_rationale

    ok = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="FIRE",
        indicator_id="ind_binary_mild",
        rationale="threshold met per official synthetic print",
        no_deliberation_reason="capability test"))
    assert ok.get("status") == "pending"
    assert topic_path.read_bytes() == before_bytes  # proposals never mutate


def test_commit_match_applies_fire_through_gates(nrol, topic_path):
    art = json.loads(nrol.submit_article(_article()))
    prop = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="FIRE",
        indicator_id="ind_binary_mild",
        rationale="threshold met per official synthetic print",
        no_deliberation_reason="capability test"))
    before = _disk_posteriors(topic_path)

    out = json.loads(nrol.commit_match(prop["id"]))
    assert out.get("status") == "committed", out
    after = _disk_posteriors(topic_path)
    assert after["H1"] > before["H1"]
    # engine rounds posteriors to 4 decimals; sum holds to ~5e-4
    assert abs(sum(after.values()) - 1.0) < 5e-4

    # A decided proposal cannot be committed again.
    again = json.loads(nrol.commit_match(prop["id"]))
    assert again.get("committed") is False
    assert "already decided" in again.get("error", "")


def test_commit_match_rejects_duplicate_url(nrol, topic_path):
    art = json.loads(nrol.submit_article(_article()))
    first = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="PARK",
        rationale="relevant, no indicator matches"))
    committed = json.loads(nrol.commit_match(first["id"]))
    assert committed.get("status") == "committed", committed

    second = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="FIRE",
        indicator_id="ind_binary_mild",
        rationale="same article again, dressed as new evidence",
        no_deliberation_reason="capability test"))
    before = _disk_posteriors(topic_path)
    out = json.loads(nrol.commit_match(second["id"]))
    assert out.get("status") == "rejected"
    assert "already committed" in out.get("error", "")
    assert _disk_posteriors(topic_path) == before


def test_commit_match_denied_without_loom_stays_pending(nrol, topic_path, monkeypatch):
    art = json.loads(nrol.submit_article(_article()))
    prop = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="PARK",
        rationale="relevant, no indicator matches"))
    monkeypatch.delenv("NROL_AO_ALLOW_UNGATED_COMMITS", raising=False)
    monkeypatch.delenv("LOOM_CONV_ID", raising=False)
    before = _disk_posteriors(topic_path)
    out = json.loads(nrol.commit_match(prop["id"]))
    assert out.get("committed") is False
    assert out.get("status") == "pending"  # denial is not rejection
    assert _disk_posteriors(topic_path) == before
    queue = json.loads(nrol.list_proposals(slug=SLUG, status="pending"))
    assert prop["id"] in [p["id"] for p in queue["proposals"]]


def test_withdraw_proposal(nrol, topic_path):
    art = json.loads(nrol.submit_article(_article()))
    prop = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="PARK",
        rationale="probably noise on reflection"))
    out = json.loads(nrol.withdraw_proposal(prop["id"], reason="noise"))
    assert out["status"] == "withdrawn"
    again = json.loads(nrol.commit_match(prop["id"]))
    assert again.get("committed") is False


# ---------------------------------------------------------------------------
# Scheduled scan with safe commit policy (spec Flow B)
# ---------------------------------------------------------------------------


def test_safe_policy_scan_parks_and_files_proposals(nrol, topic_path, monkeypatch):
    """commit_policy="safe": PARK auto-applies (no posterior movement),
    FIRE is filed as a pending proposal, a digest is written. No posterior
    moves without a human."""
    suffix = uuid.uuid4().hex[:6]
    articles = [
        {
            "headline": f"Background development {suffix}",
            "url": f"https://example.test/scan/{suffix}-a",
            "source": "test-wire", "date": "2026-06-09",
            "relevance": "relevant but matches no indicator",
        },
        {
            "headline": f"Official threshold print {suffix}",
            "url": f"https://example.test/scan/{suffix}-b",
            "source": "test-wire", "date": "2026-06-09",
            "relevance": "synthetic event A confirmed",
        },
    ]
    monkeypatch.setattr(
        nrol, "_search_web_articles",
        lambda query, channel, max_results, **kw: list(articles) if channel == "wildcard" else [],
    )
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "test-llm", "host": "local"},
    )
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "PARK"}, "tag": "EVENT",
         "claim": "relevant, unmatched", "reason": "no indicator threshold met"},
        {"idx": 2, "action": {"kind": "FIRE", "indicator_id": "ind_binary_mild"},
         "tag": "EVENT", "claim": "event A confirmed", "reason": "threshold met"},
    ])

    before = _disk_posteriors(topic_path)
    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=False, commit_policy="safe",
        fetch_full_articles=False, brief=False,
    ))
    assert "error" not in out, out
    assert _disk_posteriors(topic_path) == before  # nothing moved beliefs

    topic = _disk_topic(topic_path)
    assert len(topic["governance"]["flagged_for_indicator_review"]) >= 1  # PARK landed

    packet = out["topics"][0]
    filed = packet["commit_policy"]["proposals_filed"]
    assert len(filed) == 1  # FIRE awaits human review
    queue = json.loads(nrol.list_proposals(slug=SLUG, status="pending"))
    queued = {p["id"]: p for p in queue["proposals"]}
    assert filed[0] in queued
    assert queued[filed[0]]["action"] == "FIRE"
    assert queued[filed[0]]["indicator_id"] == "ind_binary_mild"

    assert out.get("digest_path")
    digest = Path(out["digest_path"]).read_text(encoding="utf-8")
    assert SLUG in digest
    assert "proposals filed for review: 1" in digest


def test_run_news_scan_brief_default_is_compact(nrol, topic_path, monkeypatch):
    """brief=true (default) returns a compact summary, NOT the full packet.
    No articles/excerpts/matcher_output/digest_path in the brief — those are
    the huge in-context blob that triggers sandbox break-out attempts. The
    brief carries decision counts + read-back pointers instead."""
    suffix = uuid.uuid4().hex[:6]
    articles = [
        {"headline": f"threshold print {suffix}", "url": f"https://example.test/brief/{suffix}",
         "source": "test-wire", "date": "2026-06-09", "relevance": "event A confirmed"},
    ]
    monkeypatch.setattr(nrol, "_search_web_articles",
        lambda query, channel, max_results, **kw: list(articles) if channel == "wildcard" else [])
    monkeypatch.setattr(nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "test-llm", "host": "local"})
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "FIRE", "indicator_id": "ind_binary_mild"},
         "tag": "EVENT", "claim": "event A confirmed", "reason": "threshold met"},
    ])

    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=False, commit_policy="safe",
        fetch_full_articles=False, brief=True,
    ))
    assert "error" not in out, out
    assert out.get("brief") is True
    # Compact: none of the heavy payload keys present.
    tp = out["topics"][0]
    assert "articles" not in tp
    assert "excerpts" not in tp
    assert "matcher_output" not in tp
    assert "decisions" not in tp  # only decisions_by_kind count
    # Decision tally present.
    assert tp["decisions_by_kind"].get("FIRE") == 1
    # No digest_path dangled (operator can't reach it); read-back guidance instead.
    assert "digest_path" not in out
    assert out.get("digest_available") is True
    assert "read_scan_run" in out.get("read_back", "")

    # brief=False returns the full packet (heavy payload present).
    full = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=True, commit_policy="safe",
        fetch_full_articles=False, brief=False,
    ))
    assert "brief" not in full
    assert "articles" in full["topics"][0] or "decisions" in full["topics"][0]


def _seed_anti_indicators(topic_path):
    """Add a correctly-inverted + a target-less anti-indicator to the fixture topic."""
    topic = json.loads(topic_path.read_text(encoding="utf-8"))
    topic["indicators"]["anti_indicators"] = [
        {  # correctly inverted: targets H1, H1 has lowest LR
            "id": "anti_h1_test_blockade",
            "desc": "test anti-indicator targeting H1",
            "status": "NOT_FIRED", "firedDate": None, "note": None,
            "posteriorEffect": "H1 -10pp; H4 +20pp.",
            "likelihoods": {"H1": 0.08, "H2": 0.45, "H3": 0.55, "H4": 0.92},
            "lr_decay": 1.0, "n_firings": 0, "resolution_class": False,
        },
        {  # no machine-checkable target (id doesn't encode one, no field)
            "id": "anti_iran_formal_decree",
            "desc": "test anti-indicator with no encoded target",
            "status": "NOT_FIRED", "firedDate": None, "note": None,
            "posteriorEffect": "H1 -10pp.",
            "likelihoods": {"H1": 0.08, "H2": 0.18, "H3": 0.45, "H4": 0.92},
            "lr_decay": 1.0, "n_firings": 0, "resolution_class": False,
        },
    ]
    topic_path.write_text(json.dumps(topic, indent=2), encoding="utf-8")


def test_anti_indicator_inversion_lint(nrol, topic_path):
    """The anti-indicator LR-inversion lint: blocks a wrong-inverted anti-indicator
    (firing would move the target H UP), warns on one with no machine-checkable
    target, and passes a correctly-inverted one."""
    _seed_anti_indicators(topic_path)
    lint_mod = nrol._import_from_repo("framework.lint_indicators")
    engine = nrol._import_from_repo("engine")
    topic = engine.load_topic(SLUG)
    flat = []
    for tier_list in (topic.get("indicators", {}).get("tiers", {}) or {}).values():
        for i in (tier_list or []):
            if isinstance(i, dict):
                flat.append(i)
    for i in (topic.get("indicators", {}).get("anti_indicators", []) or []):
        if isinstance(i, dict):
            ai = dict(i); ai["_tier"] = "anti_indicators"; flat.append(ai)
    report = lint_mod.propose_indicators_lint(topic, flat)
    all_issues = (report.get("warnings") or []) + (report.get("blockers") or [])

    # The correctly-inverted anti_h1_test_blockade (H1 lowest) -> no issue raised.
    assert not any(c.get("indicator") == "anti_h1_test_blockade" for c in all_issues)
    # The no-target anti_iran_formal_decree -> warning (can't verify inversion).
    no_tgt = [c for c in (report.get("warnings") or []) if c.get("check") == "anti_indicator_no_target"]
    assert any(c.get("indicator") == "anti_iran_formal_decree" for c in no_tgt)

    # Now break the correctly-inverted one: flip LRs so H1 is HIGHEST (wrong direction).
    topic2 = engine.load_topic(SLUG)
    topic2["indicators"]["anti_indicators"][0]["likelihoods"] = {"H1": 0.92, "H2": 0.45, "H3": 0.55, "H4": 0.08}
    engine.save_topic(topic2)
    flat2 = []
    for tier_list in (topic2.get("indicators", {}).get("tiers", {}) or {}).values():
        for i in (tier_list or []):
            if isinstance(i, dict): flat2.append(i)
    for i in (topic2.get("indicators", {}).get("anti_indicators", []) or []):
        if isinstance(i, dict):
            ai = dict(i); ai["_tier"] = "anti_indicators"; flat2.append(ai)
    report2 = lint_mod.propose_indicators_lint(topic2, flat2)
    blockers = [c for c in (report2.get("blockers") or []) if c.get("check") == "anti_indicator_wrong_inversion"]
    assert any(b.get("indicator") == "anti_h1_test_blockade" for b in blockers)
    assert report2["passed"] is False


def test_anti_indicator_inversion_lint_multi_target(nrol, topic_path):
    """Multi-target anti-indicator (target_hypothesis is a list): every target
    H must be at or below the median LR (suppress-half). Correctly-inverted
    multi-target passes; one with a target above the median is blocked."""
    topic = json.loads(topic_path.read_text(encoding="utf-8"))
    topic["indicators"]["anti_indicators"] = [
        {  # correctly inverted multi-target: suppresses H1/H2/H3, lifts H4
            "id": "anti_blockade_multi", "_tier": "anti_indicators",
            "desc": "blockade suppresses all reopen", "status": "NOT_FIRED",
            "target_hypothesis": ["H1", "H2", "H3"],
            "likelihoods": {"H1": 0.08, "H2": 0.18, "H3": 0.45, "H4": 0.92},
        },
        {  # WRONG: H3 is a target but its LR (0.92) exceeds non-target H4 (0.55) — firing lifts H3 above H4
            "id": "anti_blockade_bad_multi", "_tier": "anti_indicators",
            "desc": "mis-authored multi-target", "status": "NOT_FIRED",
            "target_hypothesis": ["H1", "H2", "H3"],
            "likelihoods": {"H1": 0.08, "H2": 0.18, "H3": 0.92, "H4": 0.55},
        },
    ]
    topic_path.write_text(json.dumps(topic, indent=2), encoding="utf-8")
    lint_mod = nrol._import_from_repo("framework.lint_indicators")
    engine = nrol._import_from_repo("engine")
    t = engine.load_topic(SLUG)
    flat = [dict(i) for i in (t.get("indicators", {}).get("anti_indicators", []) or [])]
    report = lint_mod.propose_indicators_lint(t, flat)
    blockers = [b for b in (report.get("blockers") or []) if b.get("check") == "anti_indicator_wrong_inversion"]
    # The good multi-target passes (no blocker).
    assert not any(b.get("indicator") == "anti_blockade_multi" for b in blockers)
    # The bad one (H3 above median) is blocked.
    assert any(b.get("indicator") == "anti_blockade_bad_multi" for b in blockers)
    assert report["passed"] is False


def test_run_news_scan_brief_tallies_anti_fire(nrol, topic_path, monkeypatch):
    """brief tallies an anti-indicator FIRE as ANTI_FIRE (distinct visibility),
    not collapsed into FIRE. Anti-indicators are not a distinct posterior
    semantic — they move posteriors through the same bayesian_update path as
    tier indicators, with LRs applied verbatim. The relabel exists for
    falsification-evidence visibility: an ANTI_FIRE is evidence against its
    target hypothesis, read as falsification, not hypothesis-strengthening."""
    _seed_anti_indicators(topic_path)
    suffix = uuid.uuid4().hex[:6]
    articles = [
        {"headline": f"blockade decree {suffix}", "url": f"https://example.test/anti/{suffix}",
         "source": "test-wire", "date": "2026-06-09", "relevance": "formal blockade"},
    ]
    monkeypatch.setattr(nrol, "_search_web_articles",
        lambda query, channel, max_results, **kw: list(articles) if channel == "wildcard" else [])
    monkeypatch.setattr(nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "test-llm", "host": "local"})
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "FIRE", "indicator_id": "anti_h1_test_blockade"},
         "tag": "EVENT", "claim": "formal blockade", "reason": "decree issued"},
    ])
    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=False, commit_policy="safe",
        fetch_full_articles=False, brief=True,
    ))
    tp = out["topics"][0]
    assert tp["decisions_by_kind"].get("ANTI_FIRE") == 1
    assert "FIRE" not in tp["decisions_by_kind"]  # not collapsed into plain FIRE


def test_safe_policy_commit_true_still_files_posterior_movers(nrol, topic_path, monkeypatch):
    """commit=true must not override commit_policy=safe for FIRE/OBSERVE."""
    suffix = uuid.uuid4().hex[:6]
    articles = [{
        "headline": f"Official threshold print {suffix}",
        "url": f"https://example.test/scan/{suffix}-b",
        "source": "test-wire",
        "date": "2026-06-09",
        "relevance": "synthetic event A confirmed",
    }]
    monkeypatch.setattr(
        nrol, "_search_web_articles",
        lambda query, channel, max_results, **kw: list(articles) if channel == "wildcard" else [],
    )
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "test-llm", "host": "local"},
    )
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "FIRE", "indicator_id": "ind_binary_mild"},
         "tag": "EVENT", "claim": "event A confirmed", "reason": "threshold met"},
    ])

    before = _disk_posteriors(topic_path)
    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=True, dry_run=False, commit_policy="safe",
        fetch_full_articles=False, deliberate=False,
    ))
    assert "error" not in out, out
    assert _disk_posteriors(topic_path) == before
    packet = out["topics"][0]
    assert packet["commit_policy"]["commit_requested"] is True
    assert packet["commit_policy"]["posterior_movers_forced_to_review"] is True
    assert len(packet["commit_policy"]["proposals_filed"]) == 1
    assert packet["committed"] is False


def test_empty_matcher_output_is_an_error_not_a_quiet_window(nrol, topic_path, monkeypatch):
    """A reasoning model can burn the whole token budget in the think channel
    and return empty content (observed live: Qwen3.6-27B, finish_reason=length,
    content=""). That must surface as a matcher error, must NOT stamp
    lastScanned (stamping shrinks the next adaptive window and drops the
    articles forever), and must not read as a quiet news window in the digest."""
    suffix = uuid.uuid4().hex[:6]
    articles = [{
        "headline": f"Relevant development {suffix}",
        "url": f"https://example.test/empty-matcher/{suffix}",
        "source": "test-wire", "date": "2026-06-10",
        "relevance": "clearly relevant",
    }]
    monkeypatch.setattr(
        nrol, "_search_web_articles",
        lambda query, channel, max_results, **kw: list(articles) if channel == "wildcard" else [],
    )
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "", "reasoning_chars": 4096, "finish_reason": "length",
                         "model": "test-thinker", "host": "local"},
    )

    before_scanned = _disk_topic(topic_path)["meta"].get("lastScanned")
    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=False, fetch_full_articles=False,
    ))
    assert "error" not in out, out

    packet = out["topics"][0]
    assert "matcher" in packet["search_errors"]
    assert "finish_reason=length" in packet["search_errors"]["matcher"]
    assert packet["decisions"] == []
    assert packet["scan_record"]["recorded"] is False
    assert "matcher" in packet["scan_record"]["skipped_reason"]
    assert _disk_topic(topic_path)["meta"].get("lastScanned") == before_scanned

    digest = Path(out["digest_path"]).read_text(encoding="utf-8")
    assert "MATCHER FAILED" in digest
    assert "a valid result" not in digest


def test_scan_feeds_matcher_full_article_excerpts(nrol, topic_path, monkeypatch):
    """Search snippets are ~500 chars of SEO text; OBSERVE needs the numbers
    from the article body. The scan fetches each deduped article and the
    matcher prompt carries an EXCERPT block with the extracted text."""
    suffix = uuid.uuid4().hex[:6]
    articles = [{
        "headline": f"Transit data print {suffix}",
        "url": f"https://example.test/full/{suffix}",
        "source": "test-wire", "date": "2026-06-10",
        "relevance": "short snippet only",
    }]
    monkeypatch.setattr(
        nrol, "_search_web_articles",
        lambda query, channel, max_results, **kw: list(articles) if channel == "wildcard" else [],
    )
    monkeypatch.setattr(
        nrol, "_fetch_article_payload",
        lambda url, max_chars, **kw: {
            "excerpt": f"FULL BODY {suffix}: AIS shows transit at 10% of baseline."
        },
    )
    seen = {}

    def fake_chat(prompt, **kw):
        seen["prompt"] = prompt
        return {"text": "canned", "model": "t", "host": "l",
                "finish_reason": "stop", "reasoning_chars": 0}

    monkeypatch.setattr(nrol.llama_client, "chat", fake_chat)

    out = json.loads(nrol.run_news_scan(slugs=[SLUG], commit=False, dry_run=True))
    assert "error" not in out, out
    assert "EXCERPT:" in seen["prompt"]
    assert f"FULL BODY {suffix}" in seen["prompt"]
    packet = out["topics"][0]
    assert packet["excerpts"] == {
        "fetched": 1,
        "attempted_fetched": 1,
        "fetch_errors": 0,
        "metadata_only": 0,
        "of": 1,
        "prefetch_of": 1,
        "chars_cap": 2800,
    }


def test_run_news_scan_uses_precommitted_search_queries(nrol, topic_path, monkeypatch):
    """Author-curated causal/actor queries must feed the operational scan."""
    topic = _disk_topic(topic_path)
    topic["searchQueries"] = ["US Iran nuclear deal sanctions relief"]
    topic_path.write_text(json.dumps(topic, indent=2), encoding="utf-8")

    article = {
        "headline": "US and Iran reach framework agreement",
        "url": "https://example.test/upstream-deal",
        "source": "test-wire",
        "date": "2026-06-10",
        "relevance": "sanctions relief framework",
    }
    calls = []

    def fake_search(query, channel, max_results, **kw):
        calls.append((channel, query))
        if channel == "searchQueries:01":
            return [article]
        return []

    monkeypatch.setattr(nrol, "_search_web_articles", fake_search)
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "test-llm", "host": "local"},
    )
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "PARK"}, "tag": "EVENT",
         "claim": "framework agreement", "reason": "relevant upstream development"},
    ])

    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=True,
        fetch_full_articles=False, deliberate=False,
    ))
    assert "error" not in out, out
    packet = out["topics"][0]
    assert packet["queries"]["searchQueries:01"] == "US Iran nuclear deal sanctions relief"
    assert "recent period" not in packet["queries"]["wildcard"]
    surfaced = packet["articles"][0].get("article") or packet["articles"][0]
    assert surfaced["headline"] == article["headline"]
    assert any(channel == "searchQueries:01" for channel, _query in calls)


def test_run_news_scan_accepts_single_slug_string(nrol, topic_path, monkeypatch):
    """Some MCP clients send a single slug as a bare string. Treat it as one
    slug, not an iterable of characters that selects zero topics."""
    monkeypatch.setattr(nrol, "_search_web_articles", lambda query, channel, max_results, **kw: [])

    out = json.loads(nrol.run_news_scan(
        slugs=SLUG,
        commit=False,
        dry_run=True,
        fetch_full_articles=False,
        deliberate=False,
    ))
    assert "error" not in out, out
    assert out["topics_scanned"] == 1
    assert out["topics"][0]["slug"] == SLUG


def test_run_news_scan_filters_old_and_seen_articles(nrol, topic_path, monkeypatch):
    """Search retrieval can be broad; matcher input must still be fresh/novel."""
    topic = _disk_topic(topic_path)
    topic["meta"]["lastScanned"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    topic["evidenceLog"].append({
        "id": "ev_seen",
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "text": "Already seen current article",
        "url": "https://example.test/already-seen",
        "source": "test-wire",
    })
    topic["searchQueries"] = ["freshness regression query"]
    topic_path.write_text(json.dumps(topic, indent=2), encoding="utf-8")

    fresh = {
        "headline": "Fresh current article",
        "url": "https://example.test/fresh",
        "source": "test-wire",
        "date": datetime.now(timezone.utc).date().isoformat(),
        "relevance": "current development",
    }
    old = {
        "headline": "Old article resurfaces",
        "url": "https://example.test/old",
        "source": "test-wire",
        "date": "2000-01-01",
        "relevance": "stale development",
    }
    seen = {
        "headline": "Already seen current article",
        "url": "https://example.test/already-seen",
        "source": "test-wire",
        "date": datetime.now(timezone.utc).date().isoformat(),
        "relevance": "duplicate development",
    }

    def fake_search(query, channel, max_results, **kw):
        if channel == "searchQueries:01":
            return [old, seen, fresh]
        return []

    monkeypatch.setattr(nrol, "_search_web_articles", fake_search)
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "test-llm", "host": "local"},
    )
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    seen_headlines = []

    def fake_prompt(topic_arg, articles):
        seen_headlines.extend((a.get("article", a)).get("headline") for a in articles)
        return "prompt"

    monkeypatch.setattr(fw, "build_matcher_prompt", fake_prompt)
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "PARK"}, "tag": "EVENT",
         "claim": "fresh", "reason": "fresh article only"},
    ])

    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=True,
        fetch_full_articles=False, deliberate=False,
    ))
    packet = out["topics"][0]
    assert packet["raw_article_count"] == 3
    assert packet["freshness_filter"]["old_dated_dropped"] == 1
    assert packet["freshness_filter"]["prior_seen_dropped"] == 1
    assert packet["freshness_filter"]["kept"] == 1
    assert seen_headlines == ["Fresh current article"]


def test_run_news_scan_filters_relative_search_dates(nrol, topic_path, monkeypatch):
    """DDGS can return relative dates embedded in labels such as
    'Opinion3 days ago'. Treating those as undated lets stale articles through
    the freshness gate."""
    topic = _disk_topic(topic_path)
    topic["meta"]["classification"] = "ALERT"
    topic["meta"]["lastScanned"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    topic["searchQueries"] = ["relative date regression query"]
    topic_path.write_text(json.dumps(topic, indent=2), encoding="utf-8")

    old_relative = {
        "headline": "Old relative article",
        "url": "https://example.test/relative-old",
        "source": "test-wire",
        "date": "Opinion3 days ago",
        "relevance": "stale relative date",
    }
    fresh_relative = {
        "headline": "Fresh relative article",
        "url": "https://example.test/relative-fresh",
        "source": "test-wire",
        "date": "2hoursago",
        "relevance": "fresh relative date",
    }

    def fake_search(query, channel, max_results, **kw):
        if channel == "searchQueries:01":
            return [old_relative, fresh_relative]
        return []

    monkeypatch.setattr(nrol, "_search_web_articles", fake_search)
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "test-llm", "host": "local"},
    )
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    seen_headlines = []

    def fake_prompt(topic_arg, articles):
        seen_headlines.extend((a.get("article", a)).get("headline") for a in articles)
        return "prompt"

    monkeypatch.setattr(fw, "build_matcher_prompt", fake_prompt)
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "PARK"}, "tag": "EVENT",
         "claim": "fresh", "reason": "fresh article only"},
    ])

    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=True,
        fetch_full_articles=False, deliberate=False,
    ))
    packet = out["topics"][0]
    assert packet["freshness_filter"]["old_dated_dropped"] == 1
    assert packet["freshness_filter"]["dated_in_window"] == 1
    assert seen_headlines == ["Fresh relative article"]


def test_article_scan_key_strips_tracking_query_params(nrol):
    first = nrol._article_scan_key({
        "url": "https://www.example.test/news/story?utm_source=x&fbclid=abc&id=42#section"
    })
    second = nrol._article_scan_key({
        "url": "https://example.test/news/story?id=42"
    })
    assert first == second == "url::https://example.test/news/story?id=42"


def test_search_web_articles_uses_news_and_source_qualified_recall(nrol, monkeypatch):
    calls = []

    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query, max_results):
            calls.append(("text", query, max_results))
            if "site:bbc.com" in query:
                return [{
                    "title": "BBC topic relevant report",
                    "href": "https://www.bbc.com/news/world-test",
                    "body": "BBC source-qualified hit",
                }]
            if "site:aljazeera.com" in query:
                return [{
                    "title": "Al Jazeera topic relevant report",
                    "href": "https://www.aljazeera.com/news/test",
                    "body": "Al Jazeera source-qualified hit",
                }]
            return [
                {
                    "title": "Generic search hit",
                    "href": "https://example.test/generic",
                    "body": "generic text result",
                },
                {
                    "title": "Duplicate from text",
                    "href": "https://example.test/duplicate?utm_source=x",
                    "body": "same URL after canonicalization",
                },
            ][:max_results]

        def news(self, query, max_results):
            calls.append(("news", query, max_results))
            return [
                {
                    "title": "Duplicate from news",
                    "url": "https://example.test/duplicate",
                    "body": "duplicate should collapse",
                    "date": "2026-06-14",
                },
                {
                    "title": "News vertical hit",
                    "url": "https://news.example.test/story",
                    "body": "news vertical result",
                    "date": "2026-06-14",
                },
            ][:max_results]

    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=FakeDDGS))

    articles = nrol._search_web_articles("test topic latest news", "wildcard", 2)
    urls = {a["canonical_url"] for a in articles}

    assert len(articles) == 5
    assert "https://example.test/duplicate" in urls
    assert sum(1 for a in articles if a["canonical_url"] == "https://example.test/duplicate") == 1
    assert "https://bbc.com/news/world-test" in urls
    assert "https://aljazeera.com/news/test" in urls
    assert any(call[0] == "news" and call[1] == "test topic latest news" for call in calls)
    assert any("site:bbc.com" in call[1] for call in calls)
    assert any("site:aljazeera.com" in call[1] for call in calls)
    assert max(a["search_rank"] for a in articles) == len(articles)


def test_full_fetch_metadata_date_can_drop_old_undated_article(nrol, topic_path, monkeypatch):
    """If search has no date but full-text metadata does, freshness uses it."""
    topic = _disk_topic(topic_path)
    topic["meta"]["lastScanned"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    topic_path.write_text(json.dumps(topic, indent=2), encoding="utf-8")

    article = {
        "headline": "Undated old article",
        "url": "https://example.test/metadata-old",
        "source": "test-wire",
        "relevance": "old development resurfaced by search",
    }
    monkeypatch.setattr(
        nrol, "_search_web_articles",
        lambda query, channel, max_results, **kw: [article] if channel == "wildcard" else [],
    )
    monkeypatch.setattr(
        nrol, "_fetch_article_payload",
        lambda url, max_chars, **kw: {
            "published": "2000-01-01",
            "excerpt": "Old article body.",
        },
    )
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "should not be called", "model": "test-llm", "host": "local"},
    )

    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=True, fetch_full_articles=True,
        deliberate=False,
    ))
    packet = out["topics"][0]
    assert packet["raw_article_count"] == 1
    assert packet["freshness_filter"]["prefetch"]["undated_kept"] == 1
    assert packet["freshness_filter"]["old_dated_dropped"] == 1
    assert packet["articles"] == []
    assert out["article_count"] == 0


def test_full_fetch_metadata_date_can_rescue_stale_search_date(nrol, topic_path, monkeypatch):
    """A search result's date can describe an old page/syndication wrapper.
    Full-article metadata should get one chance to prove the article is fresh
    before the final matcher freshness gate runs."""
    topic = _disk_topic(topic_path)
    topic["meta"]["lastScanned"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    topic_path.write_text(json.dumps(topic, indent=2), encoding="utf-8")

    article = {
        "headline": "Stale-looking search hit",
        "url": "https://example.test/metadata-fresh",
        "source": "test-wire",
        "date": "2000-01-01",
        "relevance": "search date is stale",
    }
    monkeypatch.setattr(
        nrol, "_search_web_articles",
        lambda query, channel, max_results, **kw: [article] if channel == "wildcard" else [],
    )
    fresh_published = datetime.now(timezone.utc).isoformat(timespec="seconds")
    monkeypatch.setattr(
        nrol, "_fetch_article_payload",
        lambda url, max_chars, **kw: {
            "published": fresh_published,
            "excerpt": "Fresh article body.",
        },
    )
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "test-llm", "host": "local"},
    )
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "PARK"}, "tag": "EVENT",
         "claim": "rescued", "reason": "metadata date is fresh"},
    ])

    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=True, fetch_full_articles=True,
        deliberate=False,
    ))
    packet = out["topics"][0]
    assert packet["freshness_filter"]["prefetch"]["old_dated_kept_for_fetch"] == 1
    assert packet["freshness_filter"]["dated_in_window"] == 1
    assert packet["articles"][0]["date"] == fresh_published
    assert packet["articles"][0]["search_published"] == "2000-01-01"


def test_safe_scan_downgrades_undated_posterior_movers(nrol, topic_path, monkeypatch):
    """Undated search results can be context, but not FIRE/OBSERVE proposals."""

    suffix = uuid.uuid4().hex[:6]
    article = {
        "headline": f"Undated threshold claim {suffix}",
        "url": f"https://example.test/undated/{suffix}",
        "source": "test-wire",
        "relevance": "synthetic event A confirmed",
    }
    monkeypatch.setattr(
        nrol, "_search_web_articles",
        lambda query, channel, max_results, **kw: [article] if channel == "wildcard" else [],
    )
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "test-llm", "host": "local"},
    )
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "FIRE", "indicator_id": "ind_binary_mild"},
         "tag": "EVENT", "claim": "event A confirmed", "reason": "threshold met"},
    ])

    before = _disk_posteriors(topic_path)
    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=False, commit_policy="safe",
        fetch_full_articles=False, deliberate=False,
    ))
    assert "error" not in out, out
    assert _disk_posteriors(topic_path) == before
    packet = out["topics"][0]
    assert packet["freshness_filter"]["undated_kept"] == 1
    assert packet["commit_policy"]["proposals_filed"] == []
    audit = packet["commit_policy"]["safe_policy_audit"]
    assert audit["to_propose_count"] == 0
    assert audit["safe_to_apply_count"] == 1
    assert audit["freshness_downgrades"] == [{
        "idx": 1,
        "original_action": {"kind": "FIRE", "indicator_id": "ind_binary_mild"},
        "replacement_action": {"kind": "PARK"},
        "claim": "event A confirmed",
        "reason": (
            "freshness gate: undated search result cannot file FIRE/OBSERVE; "
            "original action was FIRE. threshold met"
        ),
    }]
    digest = Path(out["digest_path"]).read_text(encoding="utf-8")
    assert "freshness downgrades: 1" in digest
    assert "A1: FIRE -> PARK" in digest
    topic = _disk_topic(topic_path)
    assert topic["governance"]["flagged_for_indicator_review"]

    # The applied-decision result row surfaces evidence_id so the brief can
    # give the operator a row handle instead of a downgrade marker they can
    # only act on by re-reading the full digest packet.
    applied_results = packet["applied"].get("results") or []
    assert applied_results, "expected a result row for the downgraded PARK"
    result_row = next(r for r in applied_results if r.get("article") == "A1")
    parked_id = topic["governance"]["flagged_for_indicator_review"][-1]
    assert result_row.get("evidence_id") == parked_id, (
        f"result evidence_id {result_row.get('evidence_id')!r} != flagged id {parked_id!r}"
    )

    # brief=true surfaces the same evidence_id on the downgrade sample so an
    # operator can read_evidence / review_parked on the specific row without
    # re-reading the full on-disk digest. The freshness gate only downgrades
    # undated_not_previously_seen articles, so the brief scan uses a fresh
    # undated article (new URL) to produce a real downgrade end to end.
    suffix2 = uuid.uuid4().hex[:6]
    article2 = {
        "headline": f"Undated threshold claim {suffix2}",
        "url": f"https://example.test/undated/{suffix2}",
        "source": "test-wire",
        "relevance": "synthetic event A confirmed",
    }
    monkeypatch.setattr(
        nrol, "_search_web_articles",
        lambda query, channel, max_results, **kw: [article2] if channel == "wildcard" else [],
    )
    brief = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=False, commit_policy="safe",
        fetch_full_articles=False, deliberate=False, brief=True,
    ))
    samples = brief["topics"][0]["freshness_downgrade_samples"]
    assert samples, "expected at least one freshness-downgrade sample in the brief"
    assert samples[0]["evidence_id"], (
        f"brief sample missing evidence_id: {samples[0]!r}"
    )
    flagged_after = _disk_topic(topic_path)["governance"]["flagged_for_indicator_review"]
    assert samples[0]["evidence_id"] in flagged_after, (
        f"brief sample evidence_id {samples[0]['evidence_id']!r} not in flagged {flagged_after!r}"
    )


def test_scan_deliberation_rescues_park_into_proposal(nrol, topic_path, monkeypatch):
    """The strict matcher's PARK is not the last word: the advocate/rebut/
    jury debate can rescue a parked article into an OBSERVE, which lands in
    the proposal queue for human commit — recall widened, authority intact.
    Stage outputs use blank-line blocks (no END terminators), exercising the
    real stage parsers."""
    suffix = uuid.uuid4().hex[:6]
    articles = [{
        "headline": f"Metric print {suffix}",
        "url": f"https://example.test/debate/{suffix}",
        "source": "test-wire", "date": "2026-06-10",
        "relevance": "metric reported at 55 percent",
    }]
    monkeypatch.setattr(
        nrol, "_search_web_articles",
        lambda query, channel, max_results, **kw: list(articles) if channel == "wildcard" else [],
    )
    stage_outputs = [
        "DECISION\nARTICLE: A1\nACTION: PARK\nTAG: DATA\n"
        "CLAIM: metric at 55\nREASON: strict matcher unsure\n",
        "ADVOCATE\nARTICLE: A1\nVERDICT: ARGUE_MOVE\n"
        "PROPOSED_ACTION: OBSERVE ind_observable_metric AT 55\n"
        "CITE: '55 percent'\nINFERENCE: none\nREASON: directly cited value\n",
        "REBUT\nARTICLE: A1\nVERDICT: UPHOLD_MOVE\nREASON: citation is sound\n",
        "JURY\nARTICLE: A1\nVERDICT: MOVE_TO OBSERVE ind_observable_metric AT 55\n"
        "RATIONALE: cited, correct units\n",
    ]
    calls = {"n": 0}

    def staged_chat(*a, **k):
        text = stage_outputs[min(calls["n"], len(stage_outputs) - 1)]
        calls["n"] += 1
        return {"text": text, "model": "test-llm", "host": "local",
                "finish_reason": "stop", "reasoning_chars": 0}

    monkeypatch.setattr(nrol.llama_client, "chat", staged_chat)

    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=False, commit_policy="safe",
        fetch_full_articles=False,
    ))
    assert "error" not in out, out
    assert calls["n"] == 4  # matcher + advocate + rebut + jury
    packet = out["topics"][0]
    assert packet["deliberation"]["parks"] == 1
    assert packet["deliberation"]["argue_moves"] == 1
    assert packet["deliberation"]["rescued"] == 1

    rescued = packet["decisions"][0]
    assert rescued["action"]["kind"] == "OBSERVE"
    assert rescued["jury_override"] is True

    filed = packet["commit_policy"]["proposals_filed"]
    assert len(filed) == 1
    queue = json.loads(nrol.list_proposals(slug=SLUG, status="pending"))
    prop = next(p for p in queue["proposals"] if p["id"] == filed[0])
    assert prop["action"] == "OBSERVE"
    assert prop["indicator_id"] == "ind_observable_metric"

    digest = Path(out["digest_path"]).read_text(encoding="utf-8")
    assert "rescued by jury" in digest


def test_matcher_prompt_preserves_indirect_relevance(nrol, topic_path):
    prompt = nrol.build_matcher_prompt(SLUG, [{
        "headline": "Ceasefire talks affect shipping corridor",
        "url": "https://example.test/indirect-relevance",
        "source": "test-wire",
        "date": "2026-06-10",
        "relevance": "regional ceasefire compliance affects the modeled pathway",
    }])

    assert "Be strict about posterior movement" in prompt
    assert "When uncertain between" in prompt
    assert "Indirect causal pathways count as topic-relevant" in prompt


def test_scan_deliberation_can_rescue_schema_gap_into_proposal(nrol, topic_path, monkeypatch):
    suffix = uuid.uuid4().hex[:6]
    articles = [{
        "headline": f"Unmodeled metric print {suffix}",
        "url": f"https://example.test/schema-gap/{suffix}",
        "source": "test-wire",
        "date": "2026-06-10",
        "relevance": "directionally relevant metric reported at 58 percent",
    }]
    monkeypatch.setattr(
        nrol,
        "_search_web_articles",
        lambda query, channel, max_results, **kw: list(articles) if channel == "wildcard" else [],
    )
    stage_outputs = [
        "DECISION\nARTICLE: A1\nACTION: SCHEMA_GAP directionally relevant metric\n"
        "TAG: DATA\nCLAIM: metric at 58\nREASON: matcher thought schema was missing\n",
        "ADVOCATE\nARTICLE: A1\nVERDICT: ARGUE_MOVE\n"
        "PROPOSED_ACTION: OBSERVE ind_observable_metric AT 58\n"
        "CITE: '58 percent'\nINFERENCE: none\nREASON: existing observable covers it\n",
        "REBUT\nARTICLE: A1\nVERDICT: UPHOLD_MOVE\nREASON: existing metric indicator fits\n",
        "JURY\nARTICLE: A1\nVERDICT: MOVE_TO OBSERVE ind_observable_metric AT 58\n"
        "RATIONALE: schema gap was over-conservative; observable fits\n",
    ]
    calls = {"n": 0}

    def staged_chat(*a, **k):
        text = stage_outputs[min(calls["n"], len(stage_outputs) - 1)]
        calls["n"] += 1
        return {"text": text, "model": "test-llm", "host": "local",
                "finish_reason": "stop", "reasoning_chars": 0}

    monkeypatch.setattr(nrol.llama_client, "chat", staged_chat)

    before = _disk_posteriors(topic_path)
    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=False, commit_policy="safe",
        fetch_full_articles=False,
    ))
    assert "error" not in out, out
    assert _disk_posteriors(topic_path) == before
    assert calls["n"] == 4

    packet = out["topics"][0]
    assert packet["deliberation"]["schema_gaps"] == 1
    assert packet["deliberation"]["candidates"] == 1
    assert packet["deliberation"]["schema_gap_rescued"] == 1
    assert packet["decisions"][0]["action"]["kind"] == "OBSERVE"

    filed = packet["commit_policy"]["proposals_filed"]
    assert len(filed) == 1
    queue = json.loads(nrol.list_proposals(slug=SLUG, status="pending"))
    prop = next(p for p in queue["proposals"] if p["id"] == filed[0])
    assert prop["action"] == "OBSERVE"
    assert prop["indicator_id"] == "ind_observable_metric"


def test_FULL_LIFECYCLE_design_activate_evidence_posterior(nrol, nrol_repo, monkeypatch):
    """The whole system in one test: an operator designs a topic through the
    governor gates (mandatory red team included), activation refuses without
    a dynamics spec, activation succeeds with one, evidence commits, and the
    posterior moves. If this passes, 'create a topic and run it' is true
    end to end."""
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "Priors are justified by the stated design.\nVERDICT: SOUND",
                         "model": "test-red", "host": "local",
                         "finish_reason": "stop", "reasoning_chars": 0},
    )
    slug = f"lifecycle-{uuid.uuid4().hex[:6]}"
    fixture = _fixture_topic()
    hyps = {
        hk: {"label": hv["label"], "prior": hv["posterior"], "midpoint": hv["midpoint"]}
        for hk, hv in fixture["model"]["hypotheses"].items()
    }
    inds = {
        "tier1_critical": fixture["indicators"]["tiers"]["tier1_critical"],
        "anti_indicators": [],
    }

    out = json.loads(nrol.design_topic(
        slug=slug,
        title="Lifecycle test topic",
        question=fixture["meta"]["question"],
        resolution=fixture["meta"]["resolution"],
        hypotheses=hyps,
        indicators=inds,
        priors_rationale="synthetic fixture priors; H1 favored by design so directional asserts have headroom",
        resolution_date="2030-12-31",
    ))
    assert "error" not in out, out
    assert out["status"] == "DRAFT"
    assert out["red_team"]["verdict"] == "SOUND"

    # Drafts are invisible to active-topic listings
    hyp_list = json.loads(nrol.list_hypotheses(slug=slug, active_only=True))
    assert not any(t.get("slug") == slug for t in hyp_list.get("topics", []))

    # Activation refuses without a dynamics spec — time-as-evidence is not optional
    refused = json.loads(nrol.activate_topic(slug=slug))
    assert "error" in refused and "dynamics" in refused["error"].lower()

    dyn_mod = nrol._import_from_repo("framework.dynamics_shadow")
    dyn_mod.write_spec(nrol._ensure_repo(), slug, {
        "entrenched_since": "2026-06-01",
        "sustain_days": 30,
        "residual_hypothesis": "H3",
        "hypothesis_windows": {"H1": "2029-12-31", "H2": "2030-12-31"},
        "priors": {
            "lam_exit": {"alpha": 2.0, "beta_days": 480, "rationale": "synthetic"},
            "lam_ramp": {"alpha": 3.0, "beta_days": 225, "rationale": "synthetic"},
            "lam_relapse": {"alpha": 2.0, "beta_days": 240, "rationale": "synthetic"},
        },
    })

    act = json.loads(nrol.activate_topic(slug=slug))
    assert act.get("activated") is True, act

    topic_file = nrol_repo / "topics" / f"{slug}.json"
    before = _disk_posteriors(topic_file)
    res = _submit(
        nrol, slug=slug, transition="FIRE", indicator_id="ind_binary_mild",
        evidence=_evidence("Event A confirmed by test-rig",
                           url=f"https://example.test/lifecycle/{slug}"),
        commit=True,
    )
    assert res.get("committed") is True, res
    after = _disk_posteriors(topic_file)
    assert after["H1"] > before["H1"]
    assert abs(sum(after.values()) - 1.0) < 5e-4


def test_red_team_review_gates_activation(nrol, nrol_repo, monkeypatch):
    """The red team is not optional: an UNREVIEWED draft (model unreachable
    at design time) cannot activate; a REVISE verdict blocks activation
    unless the human passes the explicit logged override."""
    fixture = _fixture_topic()
    hyps = {
        hk: {"label": hv["label"], "prior": hv["posterior"], "midpoint": hv["midpoint"]}
        for hk, hv in fixture["model"]["hypotheses"].items()
    }
    inds = {"tier1_critical": fixture["indicators"]["tiers"]["tier1_critical"],
            "anti_indicators": []}
    slug = f"redgate-{uuid.uuid4().hex[:6]}"

    def _boom(*a, **k):
        raise RuntimeError("llama unreachable")

    monkeypatch.setattr(nrol.llama_client, "chat", _boom)
    out = json.loads(nrol.design_topic(
        slug=slug, title="Red gate test", question=fixture["meta"]["question"],
        resolution=fixture["meta"]["resolution"], hypotheses=hyps, indicators=inds,
        priors_rationale="synthetic priors for gate test",
        dynamics={
            "entrenched_since": "2026-06-01", "sustain_days": 30,
            "residual_hypothesis": "H3",
            "hypothesis_windows": {"H1": "2029-12-31", "H2": "2030-12-31"},
            "priors": {
                "lam_exit": {"alpha": 2.0, "beta_days": 480, "rationale": "synthetic"},
                "lam_ramp": {"alpha": 3.0, "beta_days": 225, "rationale": "synthetic"},
                "lam_relapse": {"alpha": 2.0, "beta_days": 240, "rationale": "synthetic"},
            },
        },
    ))
    assert "error" not in out, out
    assert out["red_team"]["verdict"] == "UNREVIEWED"

    refused = json.loads(nrol.activate_topic(slug=slug))
    assert "error" in refused and "UNREVIEWED" in refused["error"]

    # Model comes back, red team says REVISE
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "H1 prior looks anchored to the operator's hope.\nVERDICT: REVISE",
                         "model": "test-red", "host": "local",
                         "finish_reason": "stop", "reasoning_chars": 0},
    )
    rt = json.loads(nrol.red_team_topic(slug=slug))
    assert rt["design_review"]["verdict"] == "REVISE"

    blocked = json.loads(nrol.activate_topic(slug=slug))
    assert "error" in blocked and "REVISE" in blocked["error"]

    overridden = json.loads(nrol.activate_topic(slug=slug, accept_red_team_revise=True))
    assert overridden.get("activated") is True, overridden


def test_THE_LOOP_parked_evidence_moves_posteriors_end_to_end(nrol, topic_path, monkeypatch):
    """THE acceptance test this system lacked: evidence that was parked gets
    re-adjudicated, escalated as a rebind proposal, human-committed — and a
    POSTERIOR MOVES. No duplicate evidence entry, the parked flag resolves,
    and the update runs through every engine gate. If this fails, the system
    is not usable, whatever else passes."""
    ev_ids = _seed_parked(topic_path, n=1)
    before_posteriors = _disk_posteriors(topic_path)
    before_evidence_count = len(_disk_topic(topic_path)["evidenceLog"])

    monkeypatch.setattr(nrol, "_fetch_article_excerpt", lambda url, mc, **kw: "")
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "t", "host": "l",
                         "finish_reason": "stop", "reasoning_chars": 0},
    )
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "FIRE", "indicator_id": "ind_binary_mild"},
         "tag": "EVENT", "claim": "event A confirmed", "reason": "threshold met"},
    ])

    out = json.loads(nrol.review_parked(slug=SLUG, dry_run=False))
    assert "error" not in out, out
    assert len(out["proposals_filed"]) == 1
    pid = out["proposals_filed"][0]
    queue = json.loads(nrol.list_proposals(slug=SLUG, status="pending"))
    prop = next(p for p in queue["proposals"] if p["id"] == pid)
    assert prop["evidence_id"] == ev_ids[0]  # rebind linkage carried

    committed = json.loads(nrol.commit_match(proposal_id=pid))
    assert committed.get("status") == "committed", committed

    after = _disk_posteriors(topic_path)
    assert after != before_posteriors                 # THE POINT
    assert after["H1"] > before_posteriors["H1"]      # ind_binary_mild favors H1

    topic = _disk_topic(topic_path)
    # Rebind, not duplicate: the article's URL appears exactly once in the
    # ledger (the engine may add DECISION-ledger audit entries; those carry
    # no URL and are not evidence duplication).
    seeded_url = f"https://example.test/parked/{ev_ids[0]}"
    with_url = [e for e in topic["evidenceLog"] if e.get("url") == seeded_url]
    assert len(with_url) == 1
    assert "FIRED via rebind" in with_url[0]["posteriorImpact"]
    assert ev_ids[0] not in topic["governance"]["flagged_for_indicator_review"]  # resolved


def test_shadow_posteriors_derive_from_precommitted_dynamics(nrol, nrol_repo):
    """Shadow posteriors: first-passage probabilities from a lint-gated,
    pre-committed dynamics spec. Deterministic for fixed seed; elapsed time
    in the entrenched regime moves mass via exact Gamma conjugacy (the
    'idle month is evidence' channel). Zero authority — read-only."""
    dyn_dir = nrol_repo / "loom" / "topics" / "dynamics"
    dyn_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "slug": SLUG,
        "entrenched_since": "2026-04-23",
        "sustain_days": 30,
        "sustain_hazard_factor": 0.5,
        "residual_hypothesis": "H3",
        "hypothesis_windows": {"H1": "2026-09-30", "H2": "2027-03-31"},
        "priors": {
            "lam_exit": {"alpha": 2.0, "beta_days": 480,
                         "rationale": "synthetic reference class"},
            "lam_ramp": {"alpha": 3.0, "beta_days": 225,
                         "rationale": "synthetic ramp duration"},
            "lam_relapse": {"alpha": 2.0, "beta_days": 240,
                            "rationale": "synthetic relapse cadence"},
        },
        "evidence_nudges": [],
    }
    (dyn_dir / f"{SLUG}.dynamics.json").write_text(json.dumps(spec), encoding="utf-8")

    out1 = json.loads(nrol.shadow_posteriors(slug=SLUG, asof="2026-06-10"))
    assert "error" not in out1, out1
    post = out1["shadow_posteriors"]
    assert abs(sum(post.values()) - 1.0) < 1e-6
    assert out1["elapsed_in_entrenched_days"] == 48
    assert "SHADOW" in out1["mode"]

    # Deterministic: same spec, asof, seed -> identical numbers
    out2 = json.loads(nrol.shadow_posteriors(slug=SLUG, asof="2026-06-10"))
    assert out2["shadow_posteriors"] == post

    # Time is evidence: a later asof with no exit shifts mass toward the
    # residual (later/never) hypothesis and away from the earliest window
    late = json.loads(nrol.shadow_posteriors(slug=SLUG, asof="2026-09-01"))["shadow_posteriors"]
    assert late["H1"] < post["H1"]
    assert late["H3"] > post["H3"]

    # Lint gate: a prior without a rationale refuses to run
    spec["priors"]["lam_exit"]["rationale"] = ""
    (dyn_dir / f"{SLUG}.dynamics.json").write_text(json.dumps(spec), encoding="utf-8")
    bad = json.loads(nrol.shadow_posteriors(slug=SLUG))
    assert "error" in bad and "rationale" in bad["error"]


# ---------------------------------------------------------------------------
# Future Cast: dry-run shadow analysis (no mutation, HYPOTHETICAL, save-outside)
# ---------------------------------------------------------------------------


def _stub_red_team_approve(nrol, monkeypatch):
    """Make the red-team critique deterministic without a live llama endpoint."""
    monkeypatch.setattr(
        nrol.llama_client,
        "chat",
        lambda *a, **k: {
            "text": (
                "VERDICT: SOUND\n"
                "STRONGEST_OBJECTION: scenario is hypothetical; no real source yet.\n"
                "MISSING_EVIDENCE: named source, publication date\n"
                "RECOMMENDED_ACTION: do not commit until real confirmation exists.\n"
            ),
            "model": "test-local-llama",
            "finish_reason": "stop",
        },
    )


def test_future_cast_computes_shadow_delta_no_mutation(nrol, topic_path, monkeypatch):
    """future_cast: deep-clones the topic, applies the hypothetical through the
    engine's own bayesian_update (no save), and discards the clone. The on-disk
    topic JSON must be byte-identical before/after — this is the bayesian_update
    no-save safety gate, verified live."""
    _stub_red_team_approve(nrol, monkeypatch)
    before_disk = topic_path.read_text(encoding="utf-8")
    before_post = _disk_posteriors(topic_path)

    out = json.loads(nrol.future_cast(
        slug=SLUG,
        scenario="Synthetic event A is confirmed by a hypothetical wire report.",
        target="ind_binary_mild",
        proposed_transition="FIRE",
    ))
    assert "error" not in out, out
    assert out["status"] == "dry_run_only"
    cand = out["candidate_transitions"][0]
    assert cand["structurally_valid"] is True
    sp = cand["shadow_posteriors"]
    assert set(sp) == {"before", "after", "delta"}
    # A non-trivial FIRE should move at least one hypothesis (>1pp).
    assert any(abs(d) > 0.01 for d in sp["delta"].values()), sp["delta"]
    # Posteriors still normalize on the clone.
    assert abs(sum(sp["after"].values()) - 1.0) < 1e-6

    # SAFETY GATE: the on-disk topic is byte-identical — bayesian_update on a
    # clone wrote nothing. Posteriors, posteriorHistory, and evidenceLog are
    # all unchanged.
    assert topic_path.read_text(encoding="utf-8") == before_disk
    assert _disk_posteriors(topic_path) == before_post
    disk = _disk_topic(topic_path)
    assert disk["evidenceLog"] == []
    assert len(disk["model"]["posteriorHistory"]) == 1  # only the design-prior seed


def test_future_cast_deterministic_for_same_inputs(nrol, topic_path, monkeypatch):
    """Same scenario + target + transition -> identical shadow delta (fixed
    seed in shadow_posteriors; deterministic clone math)."""
    _stub_red_team_approve(nrol, monkeypatch)
    a = json.loads(nrol.future_cast(
        slug=SLUG, scenario="hypothetical A confirmed",
        target="ind_binary_mild", proposed_transition="FIRE",
    ))
    b = json.loads(nrol.future_cast(
        slug=SLUG, scenario="hypothetical A confirmed",
        target="ind_binary_mild", proposed_transition="FIRE",
    ))
    assert a["candidate_transitions"][0]["shadow_posteriors"]["after"] == \
           b["candidate_transitions"][0]["shadow_posteriors"]["after"]


def test_future_cast_structurally_invalid_when_indicator_missing(nrol, topic_path, monkeypatch):
    """A target that doesn't exist on the topic is reported
    structurally_invalid, not raised — future-cast is advisory."""
    _stub_red_team_approve(nrol, monkeypatch)
    out = json.loads(nrol.future_cast(
        slug=SLUG, scenario="x", target="ind_does_not_exist",
        proposed_transition="FIRE",
    ))
    cand = out["candidate_transitions"][0]
    assert cand["structurally_valid"] is False
    assert "indicator_not_found" in cand["governance"]["failures"]
    assert cand["shadow_posteriors"] is None


def test_future_cast_save_writes_outside_topic_state(nrol, topic_path, nrol_repo, monkeypatch):
    """save=true writes to future_casts/future_casts.jsonl, never to topic JSON
    or evidenceLog. A saved cast is not evidence."""
    _stub_red_team_approve(nrol, monkeypatch)
    before_disk = topic_path.read_text(encoding="utf-8")

    out = json.loads(nrol.future_cast(
        slug=SLUG, scenario="saved hypothetical",
        target="ind_binary_mild", proposed_transition="FIRE", save=True,
    ))
    assert out.get("saved_cast_id", "").startswith("fc_")
    cast_file = nrol_repo / "future_casts" / "future_casts.jsonl"
    assert cast_file.exists()
    rows = [json.loads(line) for line in cast_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["promoted_to_real_action"] is False
    assert rows[0]["slug"] == SLUG
    # Topic on disk untouched; no evidence written.
    assert topic_path.read_text(encoding="utf-8") == before_disk
    assert _disk_topic(topic_path)["evidenceLog"] == []


def test_future_cast_red_team_returns_critique(nrol, topic_path, monkeypatch):
    """The red_team field carries a parsed verdict + objection + missing evidence."""
    _stub_red_team_approve(nrol, monkeypatch)
    out = json.loads(nrol.future_cast(
        slug=SLUG, scenario="hypothetical A confirmed",
        target="ind_binary_mild", proposed_transition="FIRE",
    ))
    rt = out["red_team"]
    assert rt["verdict"] == "SOUND"
    assert "hypothetical" in rt["strongest_objection"].lower()
    assert "named source" in rt["missing_evidence"]


def test_future_cast_store_lifecycle(nrol, topic_path, nrol_repo, monkeypatch):
    """future_cast(save=true) -> list_future_casts -> get_future_cast ->
    save_future_cast (re-tag) -> withdraw_future_cast. Round-trips through
    the JSONL store; topic state never touched."""
    _stub_red_team_approve(nrol, monkeypatch)
    before_disk = topic_path.read_text(encoding="utf-8")
    # nrol_repo is session-scoped: start from a clean store so the
    # remaining_casts assertion is about THIS test's cast, not leftovers.
    store = nrol_repo / "future_casts" / "future_casts.jsonl"
    if store.exists():
        store.unlink()

    # 1. Save a cast.
    saved = json.loads(nrol.future_cast(
        slug=SLUG, scenario="store-lifecycle hypothetical",
        target="ind_binary_mild", proposed_transition="FIRE", save=True,
    ))
    cid = saved["saved_cast_id"]
    assert cid.startswith("fc_")
    assert topic_path.read_text(encoding="utf-8") == before_disk  # topic untouched

    # 2. List it.
    listed = json.loads(nrol.list_future_casts(slug=SLUG))
    assert listed["count"] >= 1
    assert any(c["cast_id"] == cid for c in listed["casts"])
    brief = next(c for c in listed["casts"] if c["cast_id"] == cid)
    assert "scenario_summary" in brief and "packet" not in brief  # brief view

    # 3. Get the full packet.
    full = json.loads(nrol.get_future_cast(cast_id=cid))
    assert full["cast_id"] == cid
    assert "packet" in full  # full record carries the packet
    assert full["promoted_to_real_action"] is False

    # 4. Re-tag it.
    retag = json.loads(nrol.save_future_cast(cast_id=cid, tags=["DIPLO", "interesting"]))
    assert "error" not in retag, retag
    assert "DIPLO" in retag["tags"]
    full2 = json.loads(nrol.get_future_cast(cast_id=cid))
    assert "DIPLO" in full2["tags"]

    # 5. Withdraw it.
    withdrawn = json.loads(nrol.withdraw_future_cast(cast_id=cid, reason="test cleanup"))
    assert withdrawn["withdrawn"] == cid
    assert withdrawn["remaining_casts"] == 0
    gone = json.loads(nrol.get_future_cast(cast_id=cid))
    assert "error" in gone
    # Topic still untouched throughout.
    assert topic_path.read_text(encoding="utf-8") == before_disk


def test_save_future_cast_refuses_unknown_cast_id(nrol, topic_path):
    """save_future_cast on a transient (unsaved) cast_id returns an error
    explaining how to actually save a packet."""
    out = json.loads(nrol.save_future_cast(cast_id="fc_transient_nope", tags=["x"]))
    assert "error" in out


def test_withdraw_future_cast_refuses_promoted(nrol, topic_path, nrol_repo, monkeypatch):
    """withdraw_future_cast refuses a cast promoted_to_real_action until the
    real proposal is withdrawn first."""
    _stub_red_team_approve(nrol, monkeypatch)
    saved = json.loads(nrol.future_cast(
        slug=SLUG, scenario="promoted cast", target="ind_binary_mild",
        proposed_transition="FIRE", save=True,
    ))
    cid = saved["saved_cast_id"]
    # Simulate promotion by editing the store directly (the MCP has no promote
    # path — promotion is a future workflow that stamps these fields).
    store = nrol_repo / "future_casts" / "future_casts.jsonl"
    rows = [json.loads(l) for l in store.read_text(encoding="utf-8").splitlines() if l.strip()]
    for r in rows:
        if r["cast_id"] == cid:
            r["promoted_to_real_action"] = True
            r["promoted_proposal_id"] = "prop_fake"
    store.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = json.loads(nrol.withdraw_future_cast(cast_id=cid))
    assert "error" in out and "promoted" in out["error"].lower()
    assert out["promoted_proposal_id"] == "prop_fake"
    # Still present (not withdrawn).
    assert "error" not in json.loads(nrol.get_future_cast(cast_id=cid))
    # Clean up: un-promote and withdraw so the session-scoped store isn't
    # polluted for later tests.
    rows = [json.loads(l) for l in store.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [r for r in rows if r["cast_id"] != cid]
    store.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
                     encoding="utf-8")


# ---------------------------------------------------------------------------
# Resolution: two-lane Brier (shadow vs committed) + after-action review
# ---------------------------------------------------------------------------


def _seed_dynamics_spec(nrol_repo, slug=SLUG):
    """Write a lint-clean dynamics spec so shadow_posteriors can reconstruct."""
    dyn_dir = nrol_repo / "loom" / "topics" / "dynamics"
    dyn_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "slug": slug,
        "entrenched_since": "2026-04-23",
        "sustain_days": 30,
        "sustain_hazard_factor": 0.5,
        "residual_hypothesis": "H3",
        "hypothesis_windows": {"H1": "2026-09-30", "H2": "2027-03-31"},
        "priors": {
            "lam_exit": {"alpha": 2.0, "beta_days": 480, "rationale": "synthetic"},
            "lam_ramp": {"alpha": 3.0, "beta_days": 225, "rationale": "synthetic"},
            "lam_relapse": {"alpha": 2.0, "beta_days": 240, "rationale": "synthetic"},
        },
        "evidence_nudges": [],
    }
    (dyn_dir / f"{slug}.dynamics.json").write_text(json.dumps(spec), encoding="utf-8")


def _seed_committed_update(topic_path):
    """Append a real committed posteriorHistory entry beyond the design prior,
    so the trajectory has a checkpoint to score. Mirrors engine bayesian_update
    output shape (nested posteriors + updateMethod)."""
    topic = json.loads(topic_path.read_text(encoding="utf-8"))
    # Move H1 up to 0.7 on 2026-06-15 (a committed update).
    for hk in topic["model"]["hypotheses"]:
        topic["model"]["hypotheses"][hk]["posterior"] = (
            0.7 if hk == "H1" else (0.2 if hk == "H2" else 0.1)
        )
    topic["model"].setdefault("posteriorHistory", []).append({
        "date": "2026-06-15",
        "timestamp": "2026-06-15T00:00:00+00:00",
        "posteriors": {"H1": 0.7, "H2": 0.2, "H3": 0.1},
        "updateMethod": "bayesian_update_indicator",
        "indicatorId": "ind_binary_mild",
        "evidenceRefs": ["ind_binary_mild"],
        "note": "synthetic committed update for resolution test",
    })
    topic["meta"]["lastUpdated"] = "2026-06-15T00:00:00+00:00"
    topic_path.write_text(json.dumps(topic, indent=2), encoding="utf-8")


def test_resolve_topic_sets_resolved_and_scores_two_lanes(nrol, topic_path, nrol_repo, monkeypatch):
    """resolve_topic: sets meta.status=RESOLVED, records the outcome, and
    computes a two-lane Brier (shadow vs committed). The shadow lane is
    reconstructed from the dynamics spec at each committed-history date."""
    _stub_red_team_approve(nrol, monkeypatch)  # AAR red-team stubbed
    _seed_dynamics_spec(nrol_repo)
    _seed_committed_update(topic_path)

    out = json.loads(nrol.resolve_topic(slug=SLUG, resolved_hypothesis="H1"))
    assert "error" not in out, out
    assert out["status"] == "RESOLVED"
    assert out["resolved_hypothesis"] == "H1"

    # Status flipped on disk.
    disk = _disk_topic(topic_path)
    assert disk["meta"]["status"] == "RESOLVED"
    assert disk["meta"]["resolvedHypothesis"] == "H1"

    # Outcome recorded in the committed-lane scoring block.
    outcomes = disk.get("predictionScoring", {}).get("outcomes", [])
    assert any(o.get("resolved") == "H1" for o in outcomes)

    # Two-lane Brier present with both lanes.
    brier = out["two_lane_brier"]
    assert brier is not None, out
    assert brier["vector_end"]["shadow"] is not None
    assert brier["vector_end"]["committed"] is not None
    # The committed lane should score well at the end (H1 was driven to 0.7,
    # H1 resolved truth): committed vector Brier is modest and finite.
    assert 0.0 <= brier["vector_end"]["committed"] <= 2.0
    assert "checkpoints" in brier and len(brier["checkpoints"]) >= 1

    # AAR red-team present with a verdict.
    assert out["red_team_aar"]["verdict"] in {"SOUND", "REVISE", "UNREVIEWED"}


def test_resolve_topic_refuses_already_resolved(nrol, topic_path, nrol_repo, monkeypatch):
    """resolve_topic: refuses if already RESOLVED (no double-resolution)."""
    _stub_red_team_approve(nrol, monkeypatch)
    _seed_dynamics_spec(nrol_repo)
    _seed_committed_update(topic_path)
    nrol.resolve_topic(slug=SLUG, resolved_hypothesis="H1")

    second = json.loads(nrol.resolve_topic(slug=SLUG, resolved_hypothesis="H1"))
    assert "error" in second
    assert "RESOLVED" in second["error"]


def test_resolve_topic_refuses_without_dynamics_spec(nrol, topic_path, nrol_repo, monkeypatch):
    """resolve_topic: proceeds with resolution but reports a shadow_error when
    no dynamics spec exists (shadow lane can't be reconstructed). The
    committed-lane scoring still completes; only the shadow comparison fails."""
    _stub_red_team_approve(nrol, monkeypatch)
    _seed_committed_update(topic_path)
    # nrol_repo is session-scoped: actively remove any spec an earlier test
    # left behind so this test's "no spec" premise actually holds.
    spec_file = nrol_repo / "loom" / "topics" / "dynamics" / f"{SLUG}.dynamics.json"
    if spec_file.exists():
        spec_file.unlink()
    out = json.loads(nrol.resolve_topic(slug=SLUG, resolved_hypothesis="H1"))
    assert out["status"] == "RESOLVED"
    assert out.get("shadow_error") is not None
    assert out["two_lane_brier"] is None


def test_resolution_brier_is_readonly(nrol, topic_path, nrol_repo, monkeypatch):
    """resolution_brier: never mutates topic state. The on-disk topic JSON is
    byte-identical before/after the call."""
    _stub_red_team_approve(nrol, monkeypatch)
    _seed_dynamics_spec(nrol_repo)
    _seed_committed_update(topic_path)
    nrol.resolve_topic(slug=SLUG, resolved_hypothesis="H1")

    before_disk = topic_path.read_text(encoding="utf-8")
    out = json.loads(nrol.resolution_brier(slug=SLUG))
    assert "error" not in out, out
    assert topic_path.read_text(encoding="utf-8") == before_disk
    # Both lanes present in the read-only recomputation.
    assert out["vector_end"]["shadow"] is not None
    assert out["vector_end"]["committed"] is not None


# ---------------------------------------------------------------------------
# Source-trust surfacing (LIVE source_db/source_ledger, read-only)
# ---------------------------------------------------------------------------


def test_source_calibration_status_topic_local_is_readonly(nrol, topic_path):
    """source_calibration_status(slug): returns a topic-local summary and does
    NOT mutate the topic or the source DB. The fixture topic carries a
    sourceCalibration.effectiveTrust map."""
    before_disk = topic_path.read_text(encoding="utf-8")
    out = json.loads(nrol.source_calibration_status(slug=SLUG))
    assert "error" not in out, out
    assert out["slug"] == SLUG
    assert "sources" in out
    assert "ledger_entries" in out
    # Read-only: disk untouched.
    assert topic_path.read_text(encoding="utf-8") == before_disk


def test_source_calibration_status_cross_topic_summary(nrol):
    """source_calibration_status() with no slug returns a cross-topic DB
    summary. Degrades gracefully when the DB is a stub/empty (the fixture
    copies a near-empty sources/source_db.json)."""
    out = json.loads(nrol.source_calibration_status())
    assert "error" not in out, out
    assert "sources_tracked" in out
    assert "db_path" in out


def test_source_profile_unknown_source_falls_back(nrol):
    """source_profile for an untracked source reports tracked=false and the
    0.50 fallback rather than raising."""
    out = json.loads(nrol.source_profile(source="definitely-not-a-real-source-xyz"))
    assert "error" not in out, out
    assert out["tracked"] is False
    assert out["fallback_trust"] == 0.50


def test_validate_source_db_returns_report(nrol):
    """validate_source_db: returns a valid/invalid report without raising,
    even against a stub DB. Read-only."""
    out = json.loads(nrol.validate_source_db())
    assert "error" not in out, out
    assert "valid" in out
    assert "sources_checked" in out
    assert isinstance(out["problems"], list)


def test_source_domain_patterns_is_readonly(nrol, topic_path):
    """source_domain_patterns: read-only cross-source analysis. Disk untouched."""
    before_disk = topic_path.read_text(encoding="utf-8")
    out = json.loads(nrol.source_domain_patterns(min_claims=1))
    assert "error" not in out, out
    # Result is the find_domain_patterns dict (domain_stats / source_variance).
    assert isinstance(out, dict)
    assert topic_path.read_text(encoding="utf-8") == before_disk


# ---------------------------------------------------------------------------
# Triage audit ledger (LIVE triage + optional saved-triage log, read-only)
# ---------------------------------------------------------------------------


def test_triage_headline_save_writes_outside_topic_state(nrol, topic_path, nrol_repo):
    """triage_headline(save=True): writes to loom/triage_log/triage_log.jsonl,
    never to topic JSON or evidenceLog. A logged triage is not evidence."""
    before_disk = topic_path.read_text(encoding="utf-8")
    out = json.loads(nrol.triage_headline(
        headline="Synthetic test headline for triage audit",
        source="test-rig", save=True, note="ws6 test",
    ))
    assert "error" not in out, out
    assert out.get("saved_triage_id", "").startswith("triage_")
    log_file = nrol_repo / "loom" / "triage_log" / "triage_log.jsonl"
    assert log_file.exists()
    rows = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["promoted_to_real_action"] is False
    assert rows[0]["note"] == "ws6 test"
    # Topic on disk untouched; no evidence written.
    assert topic_path.read_text(encoding="utf-8") == before_disk
    assert _disk_topic(topic_path)["evidenceLog"] == []


def test_triage_headline_no_save_writes_nothing(nrol, topic_path, nrol_repo):
    """triage_headline() without save writes no ledger file at all."""
    log_file = nrol_repo / "loom" / "triage_log" / "triage_log.jsonl"
    if log_file.exists():
        log_file.unlink()
    out = json.loads(nrol.triage_headline(headline="another headline", source="test-rig"))
    assert "error" not in out, out
    assert "saved_triage_id" not in out
    assert not log_file.exists()


def test_list_and_read_triage_log(nrol, topic_path, nrol_repo):
    """list_triage_log + read_triage_log round-trip a saved triage. Read-only."""
    nrol.triage_headline(headline="round-trip triage headline", source="test-rig", save=True)
    listed = json.loads(nrol.list_triage_log(limit=10))
    assert listed["count"] >= 1
    tid = listed["entries"][0]["triage_id"]
    full = json.loads(nrol.read_triage_log(triage_id=tid))
    assert full["triage_id"] == tid
    assert "matches" in full  # full record carries matches; list view carries matches_brief


# ---------------------------------------------------------------------------
# Social-media-user Brier (greenfield): per-handle forecast calibration
# ---------------------------------------------------------------------------


def test_log_social_forecast_writes_outside_topic_state(nrol, topic_path, nrol_repo):
    """log_social_forecast: writes to loom/social_forecasts/, never to topic
    JSON. The forecast is NOT evidence. Posteriors are renormalized to 1.0."""
    before_disk = topic_path.read_text(encoding="utf-8")
    out = json.loads(nrol.log_social_forecast(
        handle="testhandle.bsky.social", slug=SLUG,
        posteriors={"H1": 0.6, "H2": 0.3, "H3": 0.1}, note="ws7 test",
    ))
    assert "error" not in out, out
    assert out["forecast_id"].startswith("fc_")
    assert out["handle"] == "testhandle.bsky.social"
    # Renormalized to sum to 1.0.
    assert abs(sum(out["posteriors"].values()) - 1.0) < 1e-6
    # Stored outside topic state.
    store = nrol_repo / "loom" / "social_forecasts" / "social_forecasts.jsonl"
    assert store.exists()
    assert topic_path.read_text(encoding="utf-8") == before_disk
    assert _disk_topic(topic_path)["evidenceLog"] == []


def test_log_social_forecast_rejects_bad_distribution(nrol, topic_path):
    """log_social_forecast: refuses a non-positive / empty distribution."""
    bad = json.loads(nrol.log_social_forecast(
        handle="h", slug=SLUG, posteriors={"H1": -0.5},
    ))
    assert "error" in bad


def test_social_user_brier_scores_resolved_topic(nrol, topic_path, nrol_repo, monkeypatch):
    """social_user_brier: scores a handle's forecast against a RESOLVED topic's
    truth. Unresolved forecasts are reported as pending. Read-only on topic state."""
    _stub_red_team_approve(nrol, monkeypatch)
    _seed_dynamics_spec(nrol_repo)
    _seed_committed_update(topic_path)
    # Log a forecast BEFORE resolving (so it's a genuine ex-ante forecast).
    nrol.log_social_forecast(
        handle="seer.bsky.social", slug=SLUG,
        posteriors={"H1": 0.7, "H2": 0.2, "H3": 0.1},
    )
    # Before resolution: forecast is pending.
    pre = json.loads(nrol.social_user_brier(handle="seer.bsky.social", slug=SLUG))
    assert pre["pending"] == 1 and pre["scored"] == 0
    # Resolve the topic to H1.
    nrol.resolve_topic(slug=SLUG, resolved_hypothesis="H1")
    # After resolution: the forecast is scored.
    post = json.loads(nrol.social_user_brier(handle="seer.bsky.social", slug=SLUG))
    assert post["scored"] == 1 and post["pending"] == 0
    assert post["avg_brier"] is not None
    assert 0.0 <= post["avg_brier"] <= 1.0
    assert post["scored_forecasts"][0]["resolved_hypothesis"] == "H1"


def test_list_social_handles(nrol, topic_path, nrol_repo):
    """list_social_handles: lists handles with forecast counts. Read-only."""
    nrol.log_social_forecast(handle="a.bsky", slug=SLUG, posteriors={"H1": 0.5, "H2": 0.5})
    out = json.loads(nrol.list_social_handles())
    assert "error" not in out, out
    assert any(h["handle"] == "a.bsky" for h in out["handles"])


# ---------------------------------------------------------------------------
# Parked-queue re-adjudication: kept-but-timestamped + reverse staleness
# ---------------------------------------------------------------------------


def _seed_parked(topic_path, n=2):
    """Append n parked evidence entries to the fixture topic on disk."""
    topic = json.loads(topic_path.read_text(encoding="utf-8"))
    ids = []
    for i in range(n):
        ev_id = f"ev_test_{uuid.uuid4().hex[:6]}"
        topic.setdefault("evidenceLog", []).append({
            "id": ev_id,
            "time": "2026-05-07T12:00:00+00:00",
            "tag": "EVENT",
            "text": f"Parked development number {i}: threshold-adjacent report.",
            "url": f"https://example.test/parked/{ev_id}",
            "source": "test-wire",
        })
        topic.setdefault("governance", {}).setdefault(
            "flagged_for_indicator_review", []
        ).append(ev_id)
        ids.append(ev_id)
    topic_path.write_text(json.dumps(topic, indent=2), encoding="utf-8")
    return ids


def test_parse_matcher_output_handles_blank_line_blocks(nrol):
    """Models separate DECISION blocks with blank lines, no END terminator.
    The old parser required a literal END and lazily scanned until it found
    'end' as a substring of ordinary words, swallowing whole blocks (live
    failure: 12 blocks emitted, 1 parsed). Both formats must parse fully."""
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    text = (
        "DECISION\nARTICLE: A1\nACTION: FIRE ind_a\nTAG: EVENT\nCLAIM: c1\nREASON: r1\n\n"
        "DECISION\nARTICLE: A2\nACTION: OBSERVE ind_b AT 17\nTAG: DATA\nCLAIM: c2\nREASON: r2\n\n"
        "DECISION\nARTICLE: A3\nACTION: PARK\nTAG: EVENT\nCLAIM: c3\nREASON: r3\n"
    )
    ds = fw.parse_matcher_output(text)
    assert [d["idx"] for d in ds] == [1, 2, 3]
    assert ds[0]["action"]["kind"] == "FIRE"
    assert ds[1]["action"]["kind"] == "OBSERVE"
    assert float(ds[1]["action"]["value"]) == 17.0
    assert ds[2]["action"]["kind"] == "PARK"

    legacy = "DECISION\nARTICLE: A1\nACTION: PARK\nTAG: EVENT\nCLAIM: c\nREASON: r\nEND\n"
    assert len(fw.parse_matcher_output(legacy)) == 1


def test_review_parked_timestamps_without_clearing(nrol, topic_path, monkeypatch):
    """Kept-but-timestamped: re-reviewed items stay in the flagged queue but
    gain review records; FIRE re-decisions escalate to pending proposals
    (human commit); an immediate second run finds nothing due (refractory)."""
    ev_ids = _seed_parked(topic_path, n=2)
    monkeypatch.setattr(
        nrol, "_fetch_article_excerpt",
        lambda url, max_chars, **kw: "FULL TEXT: event A confirmed at threshold.",
    )
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "test-llm", "host": "local",
                         "finish_reason": "stop", "reasoning_chars": 0},
    )
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "FIRE", "indicator_id": "ind_binary_mild"},
         "tag": "EVENT", "claim": "event A confirmed", "reason": "threshold met"},
        {"idx": 2, "action": {"kind": "PARK"},
         "tag": "EVENT", "claim": "still nothing extractable", "reason": "no threshold"},
    ])

    out = json.loads(nrol.review_parked(slug=SLUG, dry_run=False))
    assert "error" not in out, out
    assert out["considered"] == 2

    topic = _disk_topic(topic_path)
    flagged = topic["governance"]["flagged_for_indicator_review"]
    for ev_id in ev_ids:
        assert ev_id in flagged  # kept, never cleared here
    book = topic["governance"]["parked_reviews"]
    assert book[ev_ids[0]]["last_decision"] == "FIRE"
    assert book[ev_ids[1]]["last_decision"] == "PARK"
    assert book[ev_ids[0]]["review_count"] == 1
    assert book[ev_ids[0]]["history"][-1]["escalated_proposal_id"]

    # FIRE escalated to a pending proposal, posteriors untouched by review
    assert len(out["proposals_filed"]) == 1
    queue = json.loads(nrol.list_proposals(slug=SLUG, status="pending"))
    assert any(p["id"] == out["proposals_filed"][0] for p in queue["proposals"])

    # Refractory: immediately re-running finds nothing due
    again = json.loads(nrol.review_parked(slug=SLUG, dry_run=False))
    assert again["considered"] == 0
    assert again["debt"]["due_count"] == 0

    # Debt accounting moved from due to fresh
    assert out["debt_after"]["fresh_count"] >= 2


def test_review_parked_cross_day_duplicate_suppresses_proposal(nrol, topic_path, monkeypatch):
    """check_cross_day_duplicates=true: a FIRE candidate that survives the
    mechanical suppression check but is judged a cross-day duplicate (different
    URL, same already-counted event) is SUPPRESSED — no proposal filed.
    Catches the gap the mechanical check misses."""
    _seed_parked(topic_path, n=1)
    monkeypatch.setattr(nrol, "_fetch_article_excerpt",
        lambda url, max_chars, **kw: "FULL TEXT: event A confirmed at threshold.")
    # The semantic duplicate judge returns DUPLICATE_OF for the cross-day check.
    monkeypatch.setattr(nrol.llama_client, "chat",
        lambda *a, **k: {"text": "VERDICT: DUPLICATE_OF ev_prior\nREASON: same launch, different outlet",
                         "model": "test-llm", "host": "local", "finish_reason": "stop"})
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "FIRE", "indicator_id": "ind_binary_mild"},
         "tag": "EVENT", "claim": "event A confirmed", "reason": "threshold met"},
    ])
    # Stub candidate-evidence so the judge has a prior to compare against.
    # Both the mechanical check and the cross-day judge now use 45d/12, so a
    # single default-tolerant stub covers both call shapes (limit=10 kept as
    # a tolerant default; never exercised by the mechanical check anymore).
    monkeypatch.setattr(nrol, "_candidate_duplicate_evidence",
        lambda topic, article, decision, window_days=45, max_candidates=12, limit=10, **kw: [{"evidence_id": "ev_prior", "score": 0.8, "reasons": ["same_event"]}])

    out = json.loads(nrol.review_parked(
        slug=SLUG, dry_run=False, check_cross_day_duplicates=True,
    ))
    assert "error" not in out, out
    # No proposal filed — suppressed as a cross-day duplicate.
    assert out["proposals_filed"] == []
    # The review record carries the cross-day suppression reason.
    rev = out["reviews"][0]
    assert rev.get("suppressed_proposal", "").startswith("cross_day_duplicate")


def test_review_parked_cross_day_unique_still_files(nrol, topic_path, monkeypatch):
    """check_cross_day_duplicates=true with a UNIQUE_EVENT verdict: the
    proposal is still filed (the check only suppresses on duplicate)."""
    _seed_parked(topic_path, n=1)
    # Reset the indicator to NOT_FIRED so the mechanical suppression check
    # (same_url + already-FIRED) doesn't pre-suppress before the cross-day
    # check runs — this test isolates the cross-day path, not the mechanical one.
    topic = json.loads(topic_path.read_text(encoding="utf-8"))
    for ind in topic["indicators"]["tiers"]["tier1_critical"]:
        ind["status"] = "NOT_FIRED"; ind["n_firings"] = 0; ind["firedDate"] = None
    topic_path.write_text(json.dumps(topic, indent=2), encoding="utf-8")
    monkeypatch.setattr(nrol, "_fetch_article_excerpt",
        lambda url, max_chars, **kw: "FULL TEXT: event A confirmed at threshold.")
    monkeypatch.setattr(nrol.llama_client, "chat",
        lambda *a, **k: {"text": "VERDICT: UNIQUE_EVENT\nREASON: genuinely new event",
                         "model": "test-llm", "host": "local", "finish_reason": "stop"})
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "FIRE", "indicator_id": "ind_binary_mild"},
         "tag": "EVENT", "claim": "event A confirmed", "reason": "threshold met"},
    ])
    monkeypatch.setattr(nrol, "_candidate_duplicate_evidence",
        lambda topic, article, decision, window_days=45, max_candidates=12, limit=10, **kw: [{"evidence_id": "ev_prior", "score": 0.5, "reasons": ["same_event"]}])

    out = json.loads(nrol.review_parked(
        slug=SLUG, dry_run=False, check_cross_day_duplicates=True,
    ))
    assert "error" not in out, out
    # UNIQUE_EVENT -> proposal filed normally.
    assert len(out["proposals_filed"]) == 1


def test_schema_change_makes_reviewed_items_due_again(nrol, topic_path, monkeypatch):
    """A PARK is conditioned on the schema: changing an indicator makes
    previously reviewed items due again via the fingerprint."""
    _seed_parked(topic_path, n=1)
    monkeypatch.setattr(nrol, "_fetch_article_excerpt", lambda url, max_chars, **kw: "")
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "t", "host": "l",
                         "finish_reason": "stop", "reasoning_chars": 0},
    )
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "PARK"}, "tag": "EVENT",
         "claim": "nothing", "reason": "no threshold"},
    ])
    out = json.loads(nrol.review_parked(slug=SLUG, dry_run=False))
    assert "error" not in out, out
    assert out["debt_after"]["due_count"] == 0

    # Mutate an indicator description on disk — the PARK's conditional changed
    topic = json.loads(topic_path.read_text(encoding="utf-8"))
    tiers = topic["indicators"]["tiers"]
    first_tier = next(iter(tiers.values()))
    first_tier[0]["desc"] = first_tier[0]["desc"] + " (threshold revised)"
    topic_path.write_text(json.dumps(topic, indent=2), encoding="utf-8")

    again = json.loads(nrol.review_parked(slug=SLUG, dry_run=True))
    assert again["considered"] >= 1
    status = json.loads(nrol.topic_status(slugs=[SLUG]))
    debt = status["topics"][0]["parkedReviewDebt"]
    assert debt["dueCount"] >= 1


# ---------------------------------------------------------------------------
# State/code split: NROL_AO_STATE_DIR
# ---------------------------------------------------------------------------


def test_state_dir_relocates_topics(nrol_repo, tmp_path):
    """With NROL_AO_STATE_DIR set, the engine reads and writes topics in the
    state dir, not the repo. Run in a subprocess: the engine computes its
    paths at import time, and this session already imported it repo-rooted."""
    import subprocess
    import sys as _sys

    state = tmp_path / "state_root"
    (state / "topics").mkdir(parents=True)
    (state / "topics" / f"{SLUG}.json").write_text(
        json.dumps(_fixture_topic(), indent=2), encoding="utf-8"
    )
    script = (
        "import engine, json\n"
        f"t = engine.load_topic({SLUG!r})\n"
        "engine.save_topic(t)\n"
        "print(json.dumps({'topics_dir': str(engine.TOPICS_DIR),"
        " 'slug': t['meta']['slug']}))\n"
    )
    env = {
        **os.environ,
        "NROL_AO_STATE_DIR": str(state),
        "PYTHONPATH": str(nrol_repo),
    }
    proc = subprocess.run(
        [_sys.executable, "-c", script],
        capture_output=True, text=True, env=env, cwd=str(nrol_repo), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["slug"] == SLUG
    assert Path(out["topics_dir"]) == state / "topics"
    # save_topic stamped lastUpdated in the STATE dir copy, not the repo's
    saved = json.loads((state / "topics" / f"{SLUG}.json").read_text(encoding="utf-8"))
    assert saved["meta"]["lastUpdated"] > "2026-06-01"


# ---------------------------------------------------------------------------
# Loom approval gate
# ---------------------------------------------------------------------------


def test_commit_without_loom_denied_by_default(nrol, topic_path, monkeypatch):
    """Fail-closed: no Loom context and no explicit opt-out means no commit."""
    monkeypatch.delenv("NROL_AO_ALLOW_UNGATED_COMMITS", raising=False)
    monkeypatch.delenv("LOOM_CONV_ID", raising=False)
    before = _disk_posteriors(topic_path)
    out = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=True,
    )
    assert out.get("committed") is not True
    assert "Loom" in (out.get("denied") or "")
    assert _disk_posteriors(topic_path) == before


# ---------------------------------------------------------------------------
# Simulated clock + evidence dating (synthetic-replay seam)
# ---------------------------------------------------------------------------


def test_as_of_pins_engine_clock(nrol, monkeypatch):
    engine = nrol._import_from_repo("engine")
    monkeypatch.setenv("NROL_AO_AS_OF", "2026-07-03")
    assert engine._now_iso() == "2026-07-03T00:00:00+00:00"
    # Non-UTC offsets normalize to UTC.
    monkeypatch.setenv("NROL_AO_AS_OF", "2026-07-03T05:00:00-05:00")
    assert engine._now_iso() == "2026-07-03T10:00:00+00:00"
    # A replay must never fall back to the wall clock silently.
    monkeypatch.setenv("NROL_AO_AS_OF", "not-a-date")
    with pytest.raises(ValueError):
        engine._now_dt()
    monkeypatch.delenv("NROL_AO_AS_OF")
    assert engine._now_iso() != "2026-07-03T00:00:00+00:00"


def test_evidence_entry_dates_by_publication(nrol):
    entry = nrol._evidence_entry(
        {"headline": "x", "published": "2026-07-01T00:00:00+00:00"}
    )
    assert entry["time"] == "2026-07-01T00:00:00+00:00"
    # An explicit evidence time wins over the article date.
    entry = nrol._evidence_entry({
        "headline": "x",
        "time": "2026-06-30T00:00:00+00:00",
        "published": "2026-07-01",
    })
    assert entry["time"] == "2026-06-30T00:00:00+00:00"
    # Neither present: no time key — the engine stamps now() at add_evidence.
    assert "time" not in nrol._evidence_entry({"headline": "x"})


def test_fire_under_simulated_clock_dates_state(nrol, topic_path, monkeypatch):
    monkeypatch.setenv("NROL_AO_AS_OF", "2026-07-03T12:00:00+00:00")
    out = _submit(
        nrol, slug=SLUG, transition="FIRE",
        evidence=_evidence(
            "Simulated event for the clock seam.",
            published="2026-07-02T08:00:00+00:00",
        ),
        indicator_id="ind_binary_mild", reason="clock seam test", commit=True,
    )
    assert out.get("committed") is True
    topic = _disk_topic(topic_path)
    # Evidence is dated by publication, not by commit wall-clock.
    times = [e.get("time") for e in topic["evidenceLog"]]
    assert "2026-07-02T08:00:00+00:00" in times
    # Engine-stamped state (firedDate) follows the simulated clock.
    fired = [
        ind.get("firedDate")
        for tier in topic["indicators"]["tiers"].values()
        for ind in tier
        if ind.get("id") == "ind_binary_mild"
    ]
    assert fired and fired[0].startswith("2026-07-03")


# ---------------------------------------------------------------------------
# Duplicate discipline: same-batch FIREs bundle to one firing
# ---------------------------------------------------------------------------


def test_same_batch_duplicate_fires_bundle_to_one_firing(nrol, topic_path):
    """Three duplicate articles firing one indicator must produce ONE firing
    with the duplicates parked as corroborating refs (the synthetic Meridia
    replay reproduced the live failure: one causal event filed three times)."""
    articles = [
        {"headline": f"Duplicate coverage {i} of the synthetic binary event",
         "url": f"https://test-rig/dup{i}", "source": "test-rig",
         "date": "2030-01-05"}
        for i in (1, 2, 3)
    ]
    output_text = "\n".join(
        f"DECISION\nARTICLE: A{i}\nACTION: FIRE ind_binary_mild\nTAG: EVENT\n"
        "CLAIM: The synthetic binary event occurred.\n"
        "REASON: Duplicate coverage of one causal event.\nEND"
        for i in (1, 2, 3)
    )
    out = json.loads(
        nrol.apply_matcher_output(SLUG, articles, output_text, commit=True,
                                  deliberate=False,
                                  no_deliberation_reason="capability test")
    )
    assert out.get("committed") is True, out
    summary = out["summary"]
    assert summary["fire"] == 1
    assert summary["park"] == 2
    groups = [g for g in summary["bundled_groups"] if g.get("kind") == "FIRE"]
    assert len(groups) == 1
    assert groups[0]["indicator_id"] == "ind_binary_mild"
    assert groups[0]["n_articles"] == 3
    assert len(groups[0]["secondary_refs"]) == 2

    topic = _disk_topic(topic_path)
    fired = [
        ind for tier in topic["indicators"]["tiers"].values()
        for ind in tier if ind.get("id") == "ind_binary_mild"
    ]
    assert fired[0]["n_firings"] == 1


# ---------------------------------------------------------------------------
# Deliberation gate: posterior-moving actions need a debate record or a
# loud waiver — undeliberated movement is not expressible
# ---------------------------------------------------------------------------


def test_fire_commit_refused_without_deliberation(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = json.loads(nrol.submit_transition(
        slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=True,
    ))
    assert out.get("committed") is False
    assert "deliberation" in out.get("error", "")
    assert _disk_posteriors(topic_path) == before


def test_fire_commit_with_waiver_stamps_evidence(nrol, topic_path):
    out = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=True,
        no_deliberation_reason="explicit waiver for the gate test",
    )
    assert out.get("committed") is True, out
    topic = _disk_topic(topic_path)
    waivers = [e.get("deliberationWaiver") for e in topic["evidenceLog"]
               if e.get("deliberationWaiver")]
    assert "explicit waiver for the gate test" in waivers


def test_fire_commit_with_debate_record_stamps_evidence(nrol, topic_path):
    record = {"jury_verdict": "COMMIT", "rationale": "advocate cited threshold",
              "candidates_debated": 1, "rebuttals": 1}
    out = json.loads(nrol.submit_transition(
        slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=True,
        deliberation=record,
    ))
    assert out.get("committed") is True, out
    topic = _disk_topic(topic_path)
    stamped = [e.get("deliberation") for e in topic["evidenceLog"]
               if e.get("deliberation")]
    assert any(s.get("jury_verdict") == "COMMIT" for s in stamped)


def test_park_needs_no_deliberation(nrol, topic_path):
    out = json.loads(nrol.submit_transition(
        slug=SLUG, transition="PARK", evidence=_evidence(), commit=True,
    ))
    assert out.get("committed") is True, out


def test_propose_fire_refused_without_deliberation(nrol, topic_path):
    art = json.loads(nrol.submit_article(_article()))
    out = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="FIRE",
        indicator_id="ind_binary_mild", rationale="directional case"))
    assert "deliberation" in out.get("error", "")


def test_commit_match_refuses_undeliberated_legacy_proposal(nrol, topic_path):
    # A proposal written straight into the store (as legacy rows were)
    # carries no deliberation field; the queue must not be a path around
    # the gate.
    from mcp_servers.nrol_ao import server as srv

    art = json.loads(nrol.submit_article(_article()))
    store = srv._proposal_store()
    prop = store.add_proposal(
        article_id=art["id"], slug=SLUG, action="FIRE",
        indicator_id="ind_binary_mild", rationale="legacy row",
    )
    before = _disk_posteriors(topic_path)
    out = json.loads(nrol.commit_match(prop["id"]))
    assert out.get("committed") is False
    assert "deliberation" in out.get("error", "")
    assert _disk_posteriors(topic_path) == before
    # Still pending — reviewable, not silently rejected.
    assert out.get("status") == "pending"


def test_commit_match_passes_waiver_through_to_evidence(nrol, topic_path):
    art = json.loads(nrol.submit_article(_article()))
    prop = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="FIRE",
        indicator_id="ind_binary_mild",
        rationale="threshold met per official synthetic print",
        no_deliberation_reason="gate test: waiver should ride to evidence"))
    out = json.loads(nrol.commit_match(prop["id"]))
    assert out.get("status") == "committed", out
    topic = _disk_topic(topic_path)
    waivers = [e.get("deliberationWaiver") for e in topic["evidenceLog"]
               if e.get("deliberationWaiver")]
    assert "gate test: waiver should ride to evidence" in waivers


def test_apply_matcher_output_refused_without_debate_or_waiver(nrol, topic_path):
    articles = [{"headline": "Mover with no debate", "url": "https://test-rig/nodebate",
                 "source": "test-rig", "date": "2030-01-05"}]
    output_text = (
        "DECISION\nARTICLE: A1\nACTION: FIRE ind_binary_mild\nTAG: EVENT\n"
        "CLAIM: The synthetic binary event occurred.\nREASON: threshold met.\nEND"
    )
    before = _disk_posteriors(topic_path)
    out = json.loads(nrol.apply_matcher_output(
        SLUG, articles, output_text, commit=True, deliberate=False))
    assert out.get("committed") is False
    assert "waiver" in out.get("error", "")
    assert _disk_posteriors(topic_path) == before


def test_deliberate_candidates_no_candidates_short_circuits(nrol, topic_path):
    # IGNORE-only batches have nothing to debate — no llama call, empty
    # records, usable offline.
    articles = [{"headline": "Irrelevant regional note", "url": "https://test-rig/ign",
                 "source": "test-rig", "date": "2030-01-05"}]
    output_text = (
        "DECISION\nARTICLE: A1\nACTION: IGNORE\nTAG: EVENT\n"
        "CLAIM: Not relevant.\nREASON: no indicator relates.\nEND"
    )
    out = json.loads(nrol.deliberate_candidates(SLUG, articles, output_text))
    assert out.get("error") is None or "error" not in out
    assert out["debate"].get("note") == "no candidates to deliberate"
    # An empty debate mints no gate-passing records, and an empty record
    # does not pass the gate.
    assert out["deliberation_records"] == {"1": {}}
    refusal, stamp = nrol._require_deliberation("FIRE", {}, "")
    assert refusal and not stamp


def test_design_gate_flags_duplicate_amplifier(nrol, topic_path):
    """An explicit lr_decay >= 1.0 declares operator intent that refires apply
    full LR (correct only for fire-once events); the design gate (which runs
    on every save) must warn. Runtime decay is disabled (2026-06-29), so this
    is a schema-design lint on declared intent, not runtime behavior. The
    fixture topic carries 1.0 on ind_binary_mild deliberately."""
    out = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=True,
    )
    assert out.get("committed") is True, out
    gate = _disk_topic(topic_path)["governance"]["designGate"]
    assert any("DUPLICATE AMPLIFIER" in w for w in gate["warnings"])


def test_governor_falsifiability_uses_top_level_signed_anti_indicators(nrol, topic_path):
    topic = _disk_topic(topic_path)
    for tier_items in topic["indicators"]["tiers"].values():
        for ind in tier_items:
            ind["posteriorEffect"] = "H1 +1pp; H2 +1pp; H3 +1pp."
    topic["indicators"]["anti_indicators"] = [{
        "id": "anti_all_signed",
        "desc": "Synthetic contrary evidence for every hypothesis.",
        "status": "NOT_FIRED",
        "posteriorEffect": "H1 -3pp; H2 -3pp; H3 -3pp.",
        "likelihoods": {"H1": 0.4, "H2": 0.4, "H3": 0.4},
    }]
    topic["indicators"]["tiers"].pop("anti_indicators", None)

    governor = nrol._import_from_repo("governor")
    report = governor.validate_hypotheses(topic)
    assert all(v["falsifiability"] == "YES" for v in report.values())
    assert all(v["checks"]["has_contrary_indicators"] for v in report.values())

    topic["indicators"]["anti_indicators"][0]["posteriorEffect"] = "H1 +3pp; H2 +3pp; H3 +3pp."
    report = governor.validate_hypotheses(topic)
    assert all(v["falsifiability"] == "NO" for v in report.values())
    assert not any(v["checks"]["has_contrary_indicators"] for v in report.values())


def test_news_scan_dedup_indexing_bug(nrol, topic_path):
    # 1. Park an unrelated first article (A1) to serve as a decoy
    decoy_evidence = {"text": "Decoy article text content.", "source": "decoy-wire", "tag": "INTEL"}
    res1 = _submit(nrol, slug=SLUG, transition="PARK", evidence=decoy_evidence, commit=True)
    assert res1.get("committed") is True
    decoy_ev_id = res1["evidence_id"]

    # 2. Park a second article (A2) which will be our target duplicate
    target_evidence = {
        "text": "Target article that we will deduplicate.",
        "url": "https://example.test/target",
        "source": "target-wire",
        "tag": "EVENT"
    }
    res2 = _submit(nrol, slug=SLUG, transition="PARK", evidence=target_evidence, commit=True)
    assert res2.get("committed") is True
    target_ev_id = res2["evidence_id"]
    assert target_ev_id != decoy_ev_id

    # 3. Park a third unrelated article (A3) so that A2 is NOT the last item in the evidence log!
    unrelated_evidence = {"text": "Unrelated trailing article.", "source": "unrelated-wire", "tag": "INTEL"}
    res3 = _submit(nrol, slug=SLUG, transition="PARK", evidence=unrelated_evidence, commit=True)
    assert res3.get("committed") is True
    unrelated_ev_id = res3["evidence_id"]
    assert unrelated_ev_id != target_ev_id

    # Let's verify topic structure: target_ev_id is in the log, but not at the last position.
    topic = _disk_topic(topic_path)
    assert topic["evidenceLog"][-1]["id"] == unrelated_ev_id

    # 4. Now, attempt to park the target article (A2) AGAIN.
    # Since it is a duplicate by URL/text, it should be deduplicated.
    # But it should return the target_ev_id, NOT the unrelated_ev_id!
    res4 = _submit(nrol, slug=SLUG, transition="PARK", evidence=target_evidence, commit=True)
    assert res4.get("committed") is True
    assert res4["evidence_id"] == target_ev_id  # Should bind to A2, NOT A3!

    # 5. Verify that A3 (unrelated trailing article) was NOT corrupted (its posteriorImpact remained unchanged)
    topic_after = _disk_topic(topic_path)
    assert topic_after["evidenceLog"][-1]["id"] == unrelated_ev_id
    assert "flagged for indicator review" in topic_after["evidenceLog"][-1].get("posteriorImpact", "")


def test_scan_debate_failure_leaves_window_open(nrol, topic_path, monkeypatch):
    """If the debate fails, we should not stamp lastScanned."""
    suffix = uuid.uuid4().hex[:6]
    articles = [{
        "headline": f"Metric print {suffix}",
        "url": f"https://example.test/debate/{suffix}",
        "source": "test-wire", "date": "2026-06-10",
        "relevance": "metric reported at 55 percent",
    }]
    monkeypatch.setattr(
        nrol, "_search_web_articles",
        lambda query, channel, max_results, **kw: list(articles) if channel == "wildcard" else [],
    )

    calls = {"n": 0}
    def staged_chat(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"text": "DECISION\nARTICLE: A1\nACTION: PARK\nTAG: DATA\nCLAIM: metric\nREASON: strict matcher unsure\n",
                    "model": "test-llm", "host": "local", "finish_reason": "stop", "reasoning_chars": 0}
        else:
            raise ValueError("Simulated debate LLM crash")

    monkeypatch.setattr(nrol.llama_client, "chat", staged_chat)

    topic_before = _disk_topic(topic_path)
    last_scanned_before = topic_before.get("meta", {}).get("lastScanned")

    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=False, commit_policy="safe",
        fetch_full_articles=False,
    ))

    assert out["topics"][0]["scan_record"]["recorded"] is False
    assert "deliberation failed" in out["topics"][0]["scan_record"]["skipped_reason"]

    topic_after = _disk_topic(topic_path)
    assert topic_after.get("meta", {}).get("lastScanned") == last_scanned_before


def test_review_parked_debate_failure_aborts_without_recording(nrol, topic_path, monkeypatch):
    """If the debate fails during review_parked, reviews should not be recorded."""
    evidence = {"text": "Parked evidence to review.", "source": "test-wire", "tag": "INTEL"}
    res = _submit(nrol, slug=SLUG, transition="PARK", evidence=evidence, commit=True)
    assert res.get("committed") is True
    ev_id = res["evidence_id"]

    calls = {"n": 0}
    def staged_chat(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"text": "DECISION\nARTICLE: A1\nACTION: PARK\nTAG: DATA\nCLAIM: still park\nREASON: unsure\n",
                    "model": "test-llm", "host": "local", "finish_reason": "stop", "reasoning_chars": 0}
        else:
            raise ValueError("Simulated debate LLM crash during review")

    monkeypatch.setattr(nrol.llama_client, "chat", staged_chat)

    out = json.loads(nrol.review_parked(slug=SLUG, dry_run=False))

    assert "error" in out
    assert "deliberation/debate failed" in out["error"]

    topic_after = _disk_topic(topic_path)
    reviews = topic_after.get("governance", {}).get("parkedReviews", [])
    assert not any(r.get("evidence_id") == ev_id for r in reviews)



# ---------------------------------------------------------------------------
# Schema-gap resolver and scan replay tools
# ---------------------------------------------------------------------------


def test_schema_gap_resolver_tools_persist_review_queue(nrol, topic_path, monkeypatch):
    _submit(
        nrol, slug=SLUG, transition="SCHEMA_GAP", evidence=_evidence(),
        reason="no observable covers this direction",
        missing_direction="operational escort gap", commit=True,
    )

    listed = json.loads(nrol.list_schema_gaps(SLUG))
    assert listed["count"] == 1
    assert listed["clusters"]

    response = """PROPOSAL
KIND: add_new_indicator
TARGET: ind_new_escort_gap
CLUSTER_ADDRESSED: operational
RATIONALE: repeated operational escort gaps need a conservative home
SCHEMA:
  desc: Escort availability is formally announced.
  likelihoods: {H1: 0.6, H2: 0.5, H3: 0.35}
  observable:
    metric: test:escort_count
    family: count_event
    threshold_value: 1
    baseline: 0
    direction: higher_strengthens
END
"""
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": response, "model": "test-schema", "host": "local"},
    )
    resolved = json.loads(nrol.run_schema_gap_resolver(SLUG, persist=True))
    assert "error" not in resolved, resolved
    assert resolved["proposals"]
    queue = json.loads(nrol.list_schema_extension_proposals(SLUG))
    assert queue["count"] == 1
    marked = json.loads(nrol.mark_schema_extension_proposal(
        SLUG, 0, "rejected", note="not specific enough"))
    assert marked["proposal"]["status"] == "rejected"


def test_apply_schema_extension_proposal_extends_indicator_without_moving_posteriors(
    nrol, topic_path, monkeypatch
):
    _submit(
        nrol, slug=SLUG, transition="SCHEMA_GAP", evidence=_evidence(),
        reason="no observable covers this direction",
        missing_direction="operational escort gap", commit=True,
    )
    response = """PROPOSAL
KIND: extend_observable
TARGET: ind_binary_mild
CLUSTER_ADDRESSED: operational
RATIONALE: repeated operational escort gaps need a conservative home
SCHEMA:
  desc: <unchanged>
  observable:
    metric: test:escort_count
    family: count_event
    threshold_value: 1
    baseline: 0
    direction: higher_strengthens
END
"""
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": response, "model": "test-schema", "host": "local"},
    )
    resolved = json.loads(nrol.run_schema_gap_resolver(SLUG, persist=True))
    assert resolved["proposals"]
    before = _disk_posteriors(topic_path)

    review_text = """VERDICT: APPROVE
RISK: low; test-only observable extension is bounded
DIRECTIONALITY: threshold count higher strengthens the intended hypothesis
DUPLICATE_OR_OVERLAP: no existing observable covers escort count
RECOMMENDATION: approve
"""
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": review_text, "model": "test-red-team", "host": "local"},
    )
    reviewed = json.loads(nrol.red_team_schema_extension_proposal(SLUG, 0))
    assert reviewed["review"]["verdict"] == "APPROVE"

    marked = json.loads(nrol.mark_schema_extension_proposal(
        SLUG, 0, "approved", note="good test indicator"))
    assert marked["proposal"]["status"] == "approved"

    applied = json.loads(nrol.apply_schema_extension_proposal(
        SLUG, 0, tier="tier3_suggestive", note="apply in test"))
    assert "error" not in applied, applied
    assert applied["applied"]["target"] == "ind_binary_mild"
    assert _disk_posteriors(topic_path) == before
    topic = _disk_topic(topic_path)
    ind = next(
        ind for ind in topic["indicators"]["tiers"]["tier1_critical"]
        if ind["id"] == "ind_binary_mild"
    )
    assert ind["observable"]["metric"] == "test:escort_count"
    queue = topic["governance"]["proposed_schema_extensions"]
    assert queue[0]["status"] == "applied"


def test_red_team_schema_extension_keeps_thinking_with_adequate_budget(
    nrol, topic_path, monkeypatch
):
    """Regression: red_team_schema_extension_proposal must call llama_client.chat
    with thinking ENABLED and a token budget large enough for thinking to finish
    AND leave room for the answer.

    Qwen3.6 reasons in reasoning_content then emits VERDICT:/RISK:/... in
    message.content, sharing one budget. At 2048 the schema red-team prompt
    (full indicator inventory) couldn't finish thinking (finish_reason=length,
    0 content → parser defaulted to empty REVISE). Measured: thinking ~2860
    tokens + answer ~260, so 4096 is the safe floor. Deliberation is preserved;
    the NO_ANSWER_EMITTED guard catches any future prompt that exceeds budget.
    """
    json.loads(nrol.propose_schema_extension(
        slug=SLUG, kind="add_new_indicator", target="ind_rt_test",
        tier="tier3_suggestive", desc="rt test indicator",
        likelihoods={"H1": 0.6, "H2": 0.45, "H3": 0.35},
        observable={"metric": "test:rt", "family": "logistic",
                    "threshold_value": 0.5, "baseline": 0.1,
                    "direction": "higher_strengthens"}))

    captured = {}

    def fake_chat(*args, **kwargs):
        captured.update(kwargs)
        return {"text": "VERDICT: APPROVE\nRISK: none\nDIRECTIONALITY: aligned\n"
                        "DUPLICATE_OR_OVERLAP: none\nRECOMMENDATION: approve\n",
                "model": "test-rt", "host": "local"}

    monkeypatch.setattr(nrol.llama_client, "chat", fake_chat)
    out = json.loads(nrol.red_team_schema_extension_proposal(SLUG, 0))
    assert "error" not in out, out
    assert out["review"]["verdict"] == "APPROVE"
    # Thinking stays on (deliberation preserved)...
    assert captured.get("disable_thinking") is False, captured
    # ...but the budget must be large enough for thinking + answer.
    assert captured.get("max_tokens", 0) >= 4096, captured


def test_red_team_schema_extension_empty_response_is_reported_not_silent_revise(
    nrol, topic_path, monkeypatch
):
    """When the model returns empty content (the thinking-budget-exhaustion
    failure shape), the review must not masquerade as a REVISE verdict with
    empty rationale. It should be clearly marked so an operator isn't fooled
    into treating a non-answer as a substantive review."""
    json.loads(nrol.propose_schema_extension(
        slug=SLUG, kind="add_new_indicator", target="ind_rt_empty",
        tier="tier3_suggestive", desc="rt empty test",
        likelihoods={"H1": 0.6, "H2": 0.45, "H3": 0.35},
        observable={"metric": "test:rte", "family": "logistic",
                    "threshold_value": 0.5, "baseline": 0.1,
                    "direction": "higher_strengthens"}))

    # The failure shape: empty content, model exhausted budget in reasoning.
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "", "model": "test-rt", "host": "local",
                         "finish_reason": "length", "reasoning_chars": 6533},
    )
    out = json.loads(nrol.red_team_schema_extension_proposal(SLUG, 0))
    review = out["review"]
    # Empty content must NOT silently parse to a substantive-looking REVISE.
    # The verdict defaults to REVISE (no VERDICT: token), but the tool must
    # explicitly mark it as a non-answer so an operator reruns rather than
    # acting on an empty review.
    assert review["verdict"] == "REVISE"
    assert review["risk"] == "NO_ANSWER_EMITTED"
    assert "non-answer" in review["recommendation"].lower()


def test_red_team_search_query_update_budget_and_guard(nrol, topic_path, monkeypatch):
    """Precautionary: red_team_search_query_update keeps thinking on with a
    4096 budget (enough for thinking + answer on bigger prompts), and flags
    empty model output as a non-answer rather than silently defaulting."""
    proposed = json.loads(nrol.propose_search_query_update(
        slug=SLUG, add=["synthetic event official report"],
        rationale="guard test", coverage_gaps=["coverage"]))
    proposal_id = proposed["id"]

    captured = {}

    def fake_chat(*args, **kwargs):
        captured.update(kwargs)
        return {"text": "VERDICT: APPROVE\nCOVERAGE: ok\nNEUTRALITY: ok\n"
                        "OVERFITTING: none\nNOISE: ok\nSCHEMA_AXIS: ok\n"
                        "RECOMMENDATION: approve\n",
                "model": "test-sq", "host": "local", "finish_reason": "stop"}

    monkeypatch.setattr(nrol.llama_client, "chat", fake_chat)
    out = json.loads(nrol.red_team_search_query_update(proposal_id))
    assert captured.get("disable_thinking") is False  # deliberation kept
    assert captured.get("max_tokens", 0) >= 4096  # enough for thinking + answer
    assert out["red_team"]["verdict"] == "APPROVE"

    # And the guard: empty model output is flagged, not silently defaulted.
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "", "model": "test-sq", "host": "local",
                         "finish_reason": "length", "reasoning_chars": 7000})
    out2 = json.loads(nrol.red_team_search_query_update(proposal_id))
    rt = out2["red_team"]
    assert rt["verdict"] == "REVISE"
    assert rt["risk"] == "NO_ANSWER_EMITTED"


def test_cross_day_duplicate_judge_empty_response_is_conservative(
    nrol, topic_path, monkeypatch
):
    """Precautionary: when the duplicate judge gets empty model output, it must
    default to UNCERTAIN_DUPLICATE (conservative — suppresses nothing), never
    UNIQUE_EVENT (which would let a duplicate through). The judge's own
    docstring biases toward uncertainty."""
    first = _submit(
        nrol, slug=SLUG, transition="PARK",
        evidence=_evidence("convoy launch confirmed", source="test-wire",
                           url="https://example.test/convoy-guard",
                           time="2030-01-05T08:00:00+00:00"),
        reason="seed", commit=True)

    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "", "model": "test-dup", "host": "local",
                         "finish_reason": "length", "reasoning_chars": 7000})
    out = json.loads(nrol.review_duplicate_candidate(
        SLUG,
        article={"headline": "second convoy report"},
        decision={"action": {"kind": "FIRE", "indicator_id": "ind_binary_mild"}},
    ))
    judgment = out.get("judgment") or {}
    # Conservative default on non-answer: UNCERTAIN, never UNIQUE_EVENT.
    assert judgment.get("verdict") == "UNCERTAIN_DUPLICATE", judgment
    assert "NO_ANSWER_EMITTED" in (judgment.get("reason") or "")



def test_propose_schema_extension_injects_hand_authored_indicator(nrol, topic_path):
    """An operator can file a hand-authored indicator proposal directly into the
    review queue — the path run_schema_gap_resolver (LLM) usually fills."""
    injected = json.loads(nrol.propose_schema_extension(
        slug=SLUG,
        kind="add_new_indicator",
        target="ind_injected_test",
        tier="tier3_suggestive",
        desc="Operator-authored window-specific signal.",
        rationale="Resolver cannot produce causal-reasoning indicators.",
        likelihoods={"H1": 0.6, "H2": 0.45, "H3": 0.35},
        observable={
            "metric": "test:injected", "family": "logistic",
            "threshold_value": 0.5, "baseline": 0.1, "direction": "higher_strengthens",
        },
        shape="per_event_member",
        causal_event_id="test_injected_ev",
    ))
    assert "error" not in injected, injected
    assert injected["proposal_index"] == 0
    prop = injected["proposal"]
    assert prop["kind"] == "add_new_indicator"
    assert prop["target"] == "ind_injected_test"
    assert prop["cluster_addressed"] == "operator_injected"
    assert prop["status"] == "pending_operator_review"
    # body is the YAML-ish string apply re-parses; structured dicts are ignored.
    assert "likelihoods: {H1: 0.6" in prop["body"]
    assert "metric: test:injected" in prop["body"]

    listed = json.loads(nrol.list_schema_extension_proposals(SLUG))
    assert listed["count"] == 1
    assert listed["proposals"][0]["target"] == "ind_injected_test"


def test_propose_schema_extension_validates_shape(nrol, topic_path):
    """Inject rejects bad kind, missing likelihoods, duplicate target, bad tier,
    and unknown target_hypothesis — mirrors apply's gates so the operator fails
    early instead of at apply time."""
    base = dict(
        slug=SLUG, kind="add_new_indicator", target="ind_shape_test",
        tier="tier3_suggestive",
        likelihoods={"H1": 0.6, "H2": 0.45, "H3": 0.35},
        observable={"metric": "test:x", "family": "logistic",
                    "threshold_value": 0.5, "baseline": 0.1,
                    "direction": "higher_strengthens"},
    )
    # bad kind
    bad = json.loads(nrol.propose_schema_extension(**{**base, "kind": "bogus"}))
    assert "error" in bad
    # missing likelihoods
    no_lr = json.loads(nrol.propose_schema_extension(
        slug=SLUG, kind="add_new_indicator", target="ind_no_lr",
        tier="tier3_suggestive",
        observable={"metric": "test:x", "family": "logistic",
                    "threshold_value": 0.5, "direction": "higher_strengthens"}))
    assert "error" in no_lr
    # duplicate target (ind_binary_mild is in the fixture)
    dup = json.loads(nrol.propose_schema_extension(**{**base, "target": "ind_binary_mild"}))
    assert "error" in dup
    # bad tier
    bad_tier = json.loads(nrol.propose_schema_extension(**{**base, "tier": "tier9_imaginary"}))
    assert "error" in bad_tier
    # unknown target_hypothesis
    bad_th = json.loads(nrol.propose_schema_extension(
        **{**base, "tier": "anti_indicators", "target": "anti_h1_injected",
           "target_hypothesis": "H9"}))
    assert "error" in bad_th


def test_propose_schema_extension_applies_anti_indicator_end_to_end(
    nrol, topic_path, monkeypatch
):
    """C1 integration proof: an injected anti-indicator with CORRECT inversion
    applies end-to-end; the structural inversion lint fires at apply time."""
    # The fixture's tier1 indicators both favor H1, so the directional-coverage
    # lint (each H needs an observable indicator favoring it) trips when adding
    # a new indicator. Seed H2/H3 coverage so the apply isn't blocked by an
    # unrelated set-level check.
    _seed_coverage_indicators(topic_path)

    injected = json.loads(nrol.propose_schema_extension(
        slug=SLUG,
        kind="add_new_indicator",
        target="anti_h3_injected",
        tier="anti_indicators",
        desc="Suppresses H3 when its falsification signal fires.",
        rationale="Window-specific falsification path the resolver cannot draft.",
        likelihoods={"H1": 0.6, "H2": 0.5, "H3": 0.2},  # H3 lowest = correct inversion
        observable={
            "metric": "test:anti_h3", "family": "logistic",
            "threshold_value": 0.5, "baseline": 0.1, "direction": "higher_strengthens",
        },
        shape="per_event_member",
        causal_event_id="test_anti_h3_ev",
        target_hypothesis="H3",
    ))
    assert "error" not in injected, injected
    assert injected["proposal"]["target_hypothesis"] == "H3"

    review_text = """VERDICT: APPROVE
RISK: low; test-only anti-indicator
DIRECTIONALITY: H3 carries lowest LR; firing suppresses H3
DUPLICATE_OR_OVERLAP: none
RECOMMENDATION: approve
"""
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": review_text, "model": "test-red-team", "host": "local"},
    )
    reviewed = json.loads(nrol.red_team_schema_extension_proposal(SLUG, 0))
    assert reviewed["review"]["verdict"] == "APPROVE"

    marked = json.loads(nrol.mark_schema_extension_proposal(SLUG, 0, "approved"))
    assert marked["proposal"]["status"] == "approved"

    before = _disk_posteriors(topic_path)
    applied = json.loads(nrol.apply_schema_extension_proposal(
        SLUG, 0, tier="anti_indicators", note="apply injected anti-indicator"))
    assert "error" not in applied, applied
    # posteriors untouched — apply changes schema only.
    assert _disk_posteriors(topic_path) == before
    topic = _disk_topic(topic_path)
    anti = topic["indicators"]["anti_indicators"]
    assert any(i["id"] == "anti_h3_injected" for i in anti)
    added = next(i for i in anti if i["id"] == "anti_h3_injected")
    # _tier is a transient lint annotation — must NOT persist.
    assert "_tier" not in added
    # target_hypothesis IS a persisted field on anti-indicators.
    assert added["target_hypothesis"] == "H3"


def test_apply_honors_proposal_tier_when_caller_omits_it(
    nrol, topic_path, monkeypatch
):
    """Regression: an anti-indicator filed with tier="anti_indicators" must land
    in anti_indicators even when apply_schema_extension_proposal is called
    WITHOUT an explicit tier= argument.

    Before the fix, apply defaulted tier to "tier3_suggestive" and ignored the
    tier the proposal was filed with — so a correctly-filed anti-indicator
    silently landed as a tier3 suggestive indicator, bypassing the inversion
    lint and the anti-indicator governance coverage check. This is the
    dormant-lint shape resurfacing in a new form.
    """
    _seed_coverage_indicators(topic_path)

    json.loads(nrol.propose_schema_extension(
        slug=SLUG, kind="add_new_indicator", target="anti_h3_notier_arg",
        tier="anti_indicators", desc="filed as anti; applied without tier arg",
        rationale="regression test", target_hypothesis="H3",
        likelihoods={"H1": 0.6, "H2": 0.5, "H3": 0.2},  # H3 lowest
        observable={"metric": "test:nt", "family": "logistic",
                    "threshold_value": 0.5, "baseline": 0.1,
                    "direction": "higher_strengthens"},
        shape="per_event_member", causal_event_id="nt_ev",
    ))
    review_text = "VERDICT: APPROVE\nRISK: none\nDIRECTIONALITY: ok\nDUPLICATE_OR_OVERLAP: none\nRECOMMENDATION: approve\n"
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": review_text, "model": "test-rt", "host": "local"})
    json.loads(nrol.red_team_schema_extension_proposal(SLUG, 0))
    json.loads(nrol.mark_schema_extension_proposal(SLUG, 0, "approved"))

    # NOTE: no tier= argument passed — must default to the proposal's authored tier.
    applied = json.loads(nrol.apply_schema_extension_proposal(SLUG, 0))
    assert "error" not in applied, applied
    assert applied["applied"]["tier"] == "anti_indicators", applied
    # And it actually landed in the anti_indicators list, not tier3_suggestive.
    topic = _disk_topic(topic_path)
    assert any(i["id"] == "anti_h3_notier_arg"
               for i in topic["indicators"]["anti_indicators"]), \
        "anti-indicator filed as anti_indicators landed in the wrong tier"


def test_propose_schema_extension_wrong_inversion_blocked_at_apply(
    nrol, topic_path, monkeypatch
):
    """C1 proof (dangerous direction): an injected anti-indicator whose LRs would
    move its target UP is blocked by the engine inversion lint at apply — the
    lint that was dormant on the apply path before the _tier stamp fix."""
    _seed_coverage_indicators(topic_path)

    injected = json.loads(nrol.propose_schema_extension(
        slug=SLUG,
        kind="add_new_indicator",
        target="anti_h3_wrong_inversion",
        tier="anti_indicators",
        desc="Mis-authored: H3 target but H3 has the HIGHEST LR.",
        rationale="should be blocked",
        likelihoods={"H1": 0.2, "H2": 0.5, "H3": 0.6},  # H3 highest = wrong inversion
        observable={
            "metric": "test:bad_anti", "family": "logistic",
            "threshold_value": 0.5, "baseline": 0.1, "direction": "higher_strengthens",
        },
        shape="per_event_member",
        causal_event_id="test_bad_anti_ev",
        target_hypothesis="H3",
    ))
    assert "error" not in injected

    review_text = """VERDICT: APPROVE
RISK: test skips red-team substance; engine lint is the real gate
DIRECTIONALITY: not checked by LLM here
DUPLICATE_OR_OVERLAP: none
RECOMMENDATION: approve (engine lint will block if inversion wrong)
"""
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": review_text, "model": "test-red-team", "host": "local"},
    )
    json.loads(nrol.red_team_schema_extension_proposal(SLUG, 0))
    json.loads(nrol.mark_schema_extension_proposal(SLUG, 0, "approved"))

    applied = json.loads(nrol.apply_schema_extension_proposal(
        SLUG, 0, tier="anti_indicators"))
    assert "error" in applied, applied
    assert "wrong direction" in applied["error"].lower() or "inversion" in applied["error"].lower()
    # nothing was added
    topic = _disk_topic(topic_path)
    assert not any(i["id"] == "anti_h3_wrong_inversion"
                   for i in topic["indicators"]["anti_indicators"])


def test_cross_day_duplicate_reviewer_judges_prior_evidence(nrol, topic_path, monkeypatch):
    first = _submit(
        nrol, slug=SLUG, transition="PARK",
        evidence=_evidence(
            "Coast guard confirms the convoy launch in Strait X.",
            source="test-wire",
            url="https://example.test/convoy-a",
            time="2030-01-05T08:00:00+00:00",
        ),
        reason="seed prior evidence", commit=True,
    )
    assert first.get("evidence_id")

    def fake_chat(*args, **kwargs):
        return {
            "text": f"VERDICT: DUPLICATE_OF {first['evidence_id']}\n"
                    "REASON: same convoy launch described again.",
            "model": "test-duplicate",
            "host": "local",
        }

    monkeypatch.setattr(nrol.llama_client, "chat", fake_chat)
    out = json.loads(nrol.review_duplicate_candidate(
        SLUG,
        article={
            "headline": "Second report: convoy launch confirmed in Strait X",
            "url": "https://example.test/convoy-b",
            "source": "test-wire",
            "published": "2030-01-08T08:00:00+00:00",
        },
        decision={
            "idx": 1,
            "action": {"kind": "FIRE", "indicator_id": "ind_binary_mild"},
            "claim": "The convoy launch occurred.",
            "reason": "threshold met",
        },
        window_days=14,
    ))
    assert out["judgment"]["verdict"] == "DUPLICATE_OF"
    assert out["judgment"]["evidence_id"] == first["evidence_id"]
    assert out["candidates"]


def test_safe_policy_never_applies_posterior_moving_duplicates(nrol, topic_path, monkeypatch):
    """Regression for 2026-06-12: safe scan filtered canonical decisions but
    appended duplicate-map members unfiltered, letting OBSERVE/FIRE move
    beliefs during commit_policy=safe."""

    suffix = uuid.uuid4().hex[:6]
    articles = [
        {"headline": f"Park canonical {suffix}", "url": f"https://example.test/safe-dup/{suffix}-a",
         "source": "test-wire", "date": "2026-06-09", "relevance": "background"},
        {"headline": f"Metric duplicate {suffix}", "url": f"https://example.test/safe-dup/{suffix}-b",
         "source": "test-wire", "date": "2026-06-09", "relevance": "metric at 60 percent"},
    ]
    monkeypatch.setattr(
        nrol, "_search_web_articles",
        lambda query, channel, max_results, **kw: list(articles) if channel == "wildcard" else [],
    )
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "test-llm", "host": "local"},
    )
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    canonical = {"idx": 1, "action": {"kind": "PARK"}, "tag": "EVENT",
                 "claim": "background", "reason": "no move"}
    mover = {"idx": 2, "action": {"kind": "OBSERVE", "indicator_id": "ind_observable_metric", "value": 60},
             "tag": "DATA", "claim": "metric at 60", "reason": "numeric metric"}
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [canonical, mover])
    monkeypatch.setattr(fw, "group_decisions_by_duplicates", lambda arts, decisions: ([canonical], {1: [mover]}))

    before = _disk_posteriors(topic_path)
    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=False, commit_policy="safe",
        fetch_full_articles=False, deliberate=False,
    ))
    assert "error" not in out, out
    assert _disk_posteriors(topic_path) == before
    policy = out["topics"][0]["commit_policy"]
    assert policy["auto_committed"].get("observe", 0) == 0
    assert len(policy["proposals_filed"]) == 1
    queue = json.loads(nrol.list_proposals(slug=SLUG, status="pending"))
    prop = next(p for p in queue["proposals"] if p["id"] == policy["proposals_filed"][0])
    assert prop["action"] == "OBSERVE"
    assert prop["indicator_id"] == "ind_observable_metric"


def test_replay_scan_run_dry_run_and_proposal_only(nrol, topic_path, monkeypatch):
    digest_dir = Path(os.environ["NROL_AO_ACTIVITY_DIR"]) / "digests"
    digest_dir.mkdir(parents=True, exist_ok=True)
    digest = {
        "job_id": "scan-test",
        "topics": [{
            "slug": SLUG,
            "articles": [{"headline": "Replay metric", "url": "https://example.test/replay-metric",
                           "source": "test-wire", "date": "2026-06-09"}],
            "decisions": [{"idx": 1, "action": {"kind": "OBSERVE", "indicator_id": "ind_observable_metric", "value": 55},
                            "tag": "DATA", "claim": "metric at 55", "reason": "numeric metric"}],
            "deliberation": {"candidates": 1, "rebuttals": 1, "jury_verdicts": {"1": "COMMIT OBSERVE ind_observable_metric AT 55"}},
        }],
    }
    path = digest_dir / "digest-20990101T000000Z.json"
    path.write_text(json.dumps(digest), encoding="utf-8")
    (digest_dir / "digest-20990101T000000Z.md").write_text("# test", encoding="utf-8")

    before = _disk_posteriors(topic_path)
    dry = json.loads(nrol.replay_scan_run(str(path), mode="dry_run"))
    assert dry["topics"][0]["posterior_moving_count"] == 1
    assert _disk_posteriors(topic_path) == before

    prop_only = json.loads(nrol.replay_scan_run(str(path), mode="proposal_only"))
    assert prop_only["topics"][0]["proposals_filed"]
    assert _disk_posteriors(topic_path) == before


def test_undo_scan_run_removes_dirty_activity_and_digest_records(nrol, topic_path):
    digest_dir = Path(os.environ["NROL_AO_ACTIVITY_DIR"]) / "digests"
    digest_dir.mkdir(parents=True, exist_ok=True)
    job_id = f"dirty-scan-{uuid.uuid4().hex[:8]}"
    packet = {
        "job_id": job_id,
        "topics_scanned": 1,
        "article_count": 80,
        "decision_count": 0,
        "topics": [{
            "slug": SLUG,
            "raw_article_count": 80,
            "articles": [{"headline": "Dirty", "url": "https://example.test/dirty"}],
        }],
    }
    json_path = digest_dir / f"digest-{job_id}.json"
    md_path = digest_dir / f"digest-{job_id}.md"
    json_path.write_text(json.dumps(packet), encoding="utf-8")
    md_path.write_text("# dirty", encoding="utf-8")
    nrol._activity_store().record(
        job_id,
        "completed",
        task="run_news_scan",
        response=json.dumps(packet),
        summary={"article_count": 80},
    )

    dry = json.loads(nrol.undo_scan_run(job_id=job_id, dry_run=True))
    assert dry["matched"] == 1
    assert json_path.exists()

    out = json.loads(nrol.undo_scan_run(job_id=job_id, dry_run=False))
    assert out["matched"] == 1
    assert job_id in out["removed_job_ids"]
    assert not json_path.exists()
    assert not md_path.exists()
    log_text = nrol._activity_store().log_path.read_text(encoding="utf-8")
    assert job_id not in log_text


# ---------------------------------------------------------------------------
# Snapshot size invariant
# ---------------------------------------------------------------------------

def test_snapshot_stays_small_when_transition_returns_full_topic(nrol, topic_path):
    """A FIRE transition with include_topic=True embeds the full topic in the
    tool's return value (correct), but the persisted activity snapshot must
    not carry that multi-MB topic in summary.topic. Regression guard for the
    list_activity transport-frame blowup: the snapshot is loaded whole and
    returned, so an unbounded summary.topic killed the connection.
    """
    out = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=True, include_topic=True,
    )
    assert out.get("committed") is True, out
    # The caller still gets the full topic back...
    assert "topic" in out, "include_topic=True must still return the topic to the caller"

    # ...but the persisted snapshot must drop it.
    store = nrol._activity_store()
    snap = json.loads(store.snapshot_path.read_text(encoding="utf-8"))
    for job in snap.get("jobs", []):
        summary = job.get("summary")
        if isinstance(summary, dict):
            assert "topic" not in summary, (
                f"job {job.get('job_id')} persisted summary.topic into the snapshot"
            )

    # And the whole snapshot file stays well under a transport-frame budget.
    snap_bytes = store.snapshot_path.stat().st_size
    assert snap_bytes < 256 * 1024, f"snapshot is {snap_bytes} bytes; expected < 256 KB"


# ---------------------------------------------------------------------------
# publish_black_hole_snapshot — programmatic dashboard publish
# ---------------------------------------------------------------------------


def _make_black_hole_repo(tmp_path: Path) -> Path:
    """A throwaway black-hole repo: git-init'd, with the surface dir + a bare
    remote named 'origin' on master, so commit/push can be exercised locally."""
    import subprocess as _sp

    bh = tmp_path / "black-hole"
    (bh / "surfaces" / "nrol-ao").mkdir(parents=True)
    (bh / "surfaces" / "nrol-ao" / "index.html").write_text(
        "<html>surface</html>", encoding="utf-8"
    )
    (bh / "surfaces" / "config.json").write_text("{}", encoding="utf-8")
    _sp.run(["git", "init", "-q", "-b", "master"], cwd=str(bh), check=True)
    _sp.run(["git", "config", "user.email", "test@nrol"], cwd=str(bh), check=True)
    _sp.run(["git", "config", "user.name", "Test"], cwd=str(bh), check=True)
    _sp.run(["git", "add", "."], cwd=str(bh), check=True)
    _sp.run(["git", "commit", "-q", "-m", "init surface"], cwd=str(bh), check=True)
    # bare remote
    remote = tmp_path / "black-hole-remote.git"
    _sp.run(["git", "init", "-q", "--bare", "-b", "master", str(remote)], check=True)
    _sp.run(["git", "remote", "add", "origin", str(remote)], cwd=str(bh), check=True)
    _sp.run(["git", "push", "-q", "origin", "master"], cwd=str(bh), check=True)
    return bh


def test_publish_black_hole_snapshot_regenerate_only(nrol, nrol_repo, topic_path, tmp_path):
    """commit=False writes data.json, makes no git changes, needs no gate."""
    bh = _make_black_hole_repo(tmp_path)
    nrol._DEFAULT_BLACK_HOLE_REPO = bh  # not used; env is the real path
    os.environ["NROL_AO_BLACK_HOLE_REPO"] = str(bh)

    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(bh), capture_output=True, text=True
    ).stdout.strip()

    out = json.loads(nrol.publish_black_hole_snapshot())
    assert "error" not in out, out
    assert out["regenerated"] is True
    # topic_count reflects whatever topics exist in the session-scoped fixture
    # repo (other tests may have added some); just assert it's a sane count.
    assert isinstance(out["topic_count"], int) and out["topic_count"] >= 1
    assert (bh / "surfaces" / "nrol-ao" / "data.json").is_file()

    # No commit happened: HEAD unchanged.
    after_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(bh), capture_output=True, text=True
    ).stdout.strip()
    assert before_head == after_head
    assert "commit" not in out  # commit=False → no commit block


def test_publish_black_hole_snapshot_commit_stages_only_data_json(
    nrol, nrol_repo, topic_path, tmp_path
):
    """commit=True produces a commit that touches ONLY surfaces/nrol-ao/data.json
    — never index.html or config.json."""
    bh = _make_black_hole_repo(tmp_path)
    os.environ["NROL_AO_BLACK_HOLE_REPO"] = str(bh)

    out = json.loads(nrol.publish_black_hole_snapshot(commit=True))
    assert "error" not in out, out
    assert out["commit"]["committed"] is True

    # The new commit must touch only data.json.
    changed = subprocess.run(
        ["git", "show", "--stat", "--name-only", "--format=", "HEAD"],
        cwd=str(bh), capture_output=True, text=True,
    ).stdout.strip().splitlines()
    assert changed == ["surfaces/nrol-ao/data.json"], changed


def test_publish_black_hole_snapshot_push_denied_without_gate(nrol, nrol_repo, topic_path, tmp_path):
    """push=True is fail-closed without LOOM_CONV_ID (gate opted out only via
    NROL_AO_ALLOW_UNGATED_COMMITS=1, which the fixture sets — so flip it off to
    test the deny path)."""
    bh = _make_black_hole_repo(tmp_path)
    os.environ["NROL_AO_BLACK_HOLE_REPO"] = str(bh)
    os.environ.pop("LOOM_CONV_ID", None)
    saved = os.environ.pop("NROL_AO_ALLOW_UNGATED_COMMITS", None)
    try:
        out = json.loads(nrol.publish_black_hole_snapshot(commit=True, push=True))
        assert out.get("denied"), out
        assert out["pushed"] is False
    finally:
        if saved is not None:
            os.environ["NROL_AO_ALLOW_UNGATED_COMMITS"] = saved


def test_publish_black_hole_snapshot_push_when_ungated(
    nrol, nrol_repo, topic_path, tmp_path
):
    """push=True with the gate opted out pushes to origin master."""
    bh = _make_black_hole_repo(tmp_path)
    os.environ["NROL_AO_BLACK_HOLE_REPO"] = str(bh)
    os.environ["NROL_AO_ALLOW_UNGATED_COMMITS"] = "1"

    out = json.loads(nrol.publish_black_hole_snapshot(commit=True, push=True))
    assert "error" not in out, out
    assert out["commit"]["committed"] is True
    assert out["push"]["pushed"] is True, out

    # The bare remote now has the data.json commit on master.
    remote_log = subprocess.run(
        ["git", "--git-dir", str(tmp_path / "black-hole-remote.git"), "log",
         "--name-only", "--format=", "master"],
        capture_output=True, text=True,
    ).stdout
    assert "surfaces/nrol-ao/data.json" in remote_log


