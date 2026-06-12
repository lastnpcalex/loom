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
import shutil
import uuid
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


# ---------------------------------------------------------------------------
# Read-path robustness
# ---------------------------------------------------------------------------


def test_topic_status_lists_fixture(nrol, topic_path):
    out = json.loads(nrol.topic_status())
    assert "error" not in out
    assert SLUG in [r["slug"] for r in out["topics"]]


def test_malformed_file_does_not_break_listing(nrol, nrol_repo, topic_path):
    bad = nrol_repo / "topics" / "manifest.json"
    bad.write_text(json.dumps({"topics": [SLUG]}), encoding="utf-8")
    try:
        out = json.loads(nrol.topic_status())
        assert "error" not in out
        assert SLUG in [r["slug"] for r in out["topics"]]
        assert "manifest" in [s["slug"] for s in out.get("skipped", [])]
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
    import importlib

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
        lambda query, channel, max_results: list(articles) if channel == "wildcard" else [],
    )
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "test-llm", "host": "local"},
    )
    fw = importlib.import_module("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "PARK"}, "tag": "EVENT",
         "claim": "relevant, unmatched", "reason": "no indicator threshold met"},
        {"idx": 2, "action": {"kind": "FIRE", "indicator_id": "ind_binary_mild"},
         "tag": "EVENT", "claim": "event A confirmed", "reason": "threshold met"},
    ])

    before = _disk_posteriors(topic_path)
    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=False, commit_policy="safe",
        fetch_full_articles=False,
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
        lambda query, channel, max_results: list(articles) if channel == "wildcard" else [],
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
        lambda query, channel, max_results: list(articles) if channel == "wildcard" else [],
    )
    monkeypatch.setattr(
        nrol, "_fetch_article_excerpt",
        lambda url, max_chars, **kw: f"FULL BODY {suffix}: AIS shows transit at 10% of baseline.",
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
    assert packet["excerpts"] == {"fetched": 1, "of": 1, "chars_cap": 2800}


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
        lambda query, channel, max_results: list(articles) if channel == "wildcard" else [],
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
    """Explicit lr_decay >= 1.0 is a duplicate amplifier; the design gate
    (which runs on every save) must warn. The fixture topic carries 1.0 on
    ind_binary_mild deliberately."""
    out = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=True,
    )
    assert out.get("committed") is True, out
    gate = _disk_topic(topic_path)["governance"]["designGate"]
    assert any("DUPLICATE AMPLIFIER" in w for w in gate["warnings"])


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
        lambda query, channel, max_results: list(articles) if channel == "wildcard" else [],
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
    import importlib

    suffix = uuid.uuid4().hex[:6]
    articles = [
        {"headline": f"Park canonical {suffix}", "url": f"https://example.test/safe-dup/{suffix}-a",
         "source": "test-wire", "date": "2026-06-09", "relevance": "background"},
        {"headline": f"Metric duplicate {suffix}", "url": f"https://example.test/safe-dup/{suffix}-b",
         "source": "test-wire", "date": "2026-06-09", "relevance": "metric at 60 percent"},
    ]
    monkeypatch.setattr(
        nrol, "_search_web_articles",
        lambda query, channel, max_results: list(articles) if channel == "wildcard" else [],
    )
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "test-llm", "host": "local"},
    )
    fw = importlib.import_module("framework.news_observation_pipeline")
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
